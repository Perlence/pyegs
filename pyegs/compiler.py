import ast
from collections import defaultdict, Iterable
from numbers import Number

import attr

from .ast import AST, Module, Assign, If, Const, Slot, BoolOp, BinOp, Compare, Call, Label
from .runtime import Yegik, Timer, Point, Bot, System
from .types import GameObjectRef, GameObjectMethod, GameObjectList, ListPointer, BuiltinFunction, Range


def compile(source, filename='<unknown>'):
    top = ast.parse(source, filename)

    converter = NodeConverter()
    converted_top = converter.visit(top)
    converter.scope.allocate_temporary()
    return str(converted_top)


@attr.s
class NodeConverter(ast.NodeVisitor):
    scope = attr.ib(default=attr.Factory(lambda: Scope()))
    bodies = attr.ib(default=attr.Factory(list))
    tests = attr.ib(default=attr.Factory(list))
    last_label = attr.ib(default=0)
    loop_labels = attr.ib(default=attr.Factory(list))
    current_node = attr.ib(default=None)

    def visit(self, node):
        try:
            self.current_node = node
            return super().visit(node)
        except Exception as e:
            if not hasattr(node, 'lineno') or not hasattr(node, 'col_offset'):
                raise
            self.annotate_node_position(e, node.lineno, node.col_offset)
            raise

    def annotate_node_position(self, exc, lineno, col_offset):
        old_msg = exc.args[0]
        line_col = 'line {}, column {}'.format(lineno, col_offset)
        msg = old_msg + '; ' + line_col if old_msg else line_col
        exc.args = (msg,) + exc.args[1:]
        return exc

    def generic_visit(self, node):
        raise NotImplementedError("node '{}' is not implemented yet".format(node))

    def visit_Module(self, node):
        body = []
        self.bodies.append(body)
        for stmt in node.body:
            self.visit(stmt)
        return Module(body)

    def visit_Assign(self, node):
        # TODO: Reassign lists to list pointers without allocating more memory:
        # 'x = [11, 22]; x = [11, 22]' -> 'p1z 11 p2z 22 p4z 1 p1z 11 p2z 22'
        src_slot = None
        for target in node.targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                # TODO: Implement iterable unpacking
                raise NotImplementedError('iterable unpacking is not implemented yet')
            if self.is_black_hole(target):
                continue
            if src_slot is None:
                src_slot = self.load_expr(node.value)
            dest_slot = self.store_value(target, src_slot)
            if dest_slot is None:
                continue
            self.append_to_body(Assign(dest_slot, src_slot))

    def visit_AugAssign(self, node):
        # TODO: Raise NameError if target is not defined
        src_slot = self.load_expr(node.value)
        dest_slot = self.store_value(node.target, src_slot)
        if dest_slot is None:
            return
        bin_op = BinOp(dest_slot, node.op, src_slot)
        self.append_to_body(Assign(dest_slot, bin_op))

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call):
            expr = self.load_expr(node.value)
            self.append_to_body(expr)
        else:
            raise NotImplementedError('plain expressions are not supported')

    def visit_For(self, node):
        # For(expr target, expr iter, stmt* body, stmt* orelse)
        if self.is_body_empty(node.body):
            return

        self.last_label += 1
        label = Label(self.last_label)
        self.loop_labels.append(label)

        is_black_hole = self.is_black_hole(node.target)
        iter_slot = self.load_expr(node.iter)
        if issubclass(iter_slot.type, Iterable):
            iterable = iter_slot.value

        elif issubclass(iter_slot.type, ListPointer):
            iterable = self.list_pointer_iter(iter_slot, subscript=(not is_black_hole))
        else:
            raise NotImplementedError("iterating over '{}' is not implemented yet".format(iter_slot.type))

        for src_slot in iterable:
            if not is_black_hole:
                dest_slot = self.store_value(node.target, src_slot)
                self.append_to_body(Assign(dest_slot, src_slot))
            for stmt in node.body:
                self.visit(stmt)

        self.append_to_body(label)
        self.loop_labels.pop()

    def list_pointer_iter(self, list_pointer, subscript=True):
        if not subscript:
            yield from range(list_pointer.type.capacity)
            return

        pointer_math_slot = self.scope.get_temporary(ListPointer)
        for i in range(list_pointer.type.capacity):
            yield self.load_list_subscript(list_pointer, Const(i, int), pointer_math_slot)
        self.scope.recycle_temporary(pointer_math_slot)

    def visit_Break(self, node):
        label_number = self.loop_labels[-1].number
        self.append_to_body(Slot('g', label_number, 'z', None))

    def visit_Pass(self, node):
        pass

    def visit_If(self, node):
        if self.is_body_empty(node.body) and self.is_body_empty(node.orelse):
            return
        if isinstance(node.test, (ast.Num, ast.Str, ast.NameConstant, ast.List)):
            self.optimized_if(node.test, node.body)
            if node.orelse:
                self.optimized_if(node.test, node.orelse, negate_test=True)
        else:
            self.generic_if(node)

    def optimized_if(self, test, body, negate_test=False):
        truthy = negate_test
        if self.is_body_empty(body):
            return
        if isinstance(test, ast.Num) and bool(test.n) is truthy:
            return
        elif isinstance(test, ast.Str) and bool(test.s) is truthy:
            return
        elif isinstance(test, ast.NameConstant) and bool(test.value) is truthy:
            return
        elif isinstance(test, ast.List) and bool(test.elts) is truthy:
            return
        for stmt in body:
            self.visit(stmt)

    def generic_if(self, node):
        test = self.load_expr(node.test)
        if not isinstance(test, Compare):
            test = Compare(test, ast.NotEq(), Const(False, bool))

        for stmt in self.prepare_if_stmt(test, node.body):
            self.visit(stmt)
        if node.orelse:
            for stmt in self.prepare_if_stmt(self.negate_bool(test), node.orelse):
                self.visit(stmt)

    def prepare_if_stmt(self, test, body):
        if self.is_body_empty(body):
            return
        all_tests = test
        self.tests.append(test)
        if len(self.tests) > 1:
            all_tests = BoolOp(ast.And(), self.tests[:])

        previous_body = None
        if len(self.bodies) > 1:
            previous_body = self.bodies.pop()

        if_body = []
        self.append_to_body(If(all_tests, if_body))
        self.bodies.append(if_body)

        yield from body

        self.bodies.pop()

        if previous_body is not None:
            self.bodies.append(previous_body)

        self.tests.remove(test)

    def is_body_empty(self, body):
        return all(isinstance(stmt, ast.Pass) for stmt in body)

    def load_expr(self, value):
        if isinstance(value, AST):
            return value
        elif isinstance(value, ast.Num):
            return Const(value.n, Number)
        elif isinstance(value, ast.Str):
            return Const(value.s, str)
        elif isinstance(value, ast.NameConstant):
            return Const(value.value, type(value.value))
        elif isinstance(value, ast.List):
            return self.load_list(value)
        elif isinstance(value, ast.Name):
            return self.scope.get(value.id)
        elif isinstance(value, ast.Attribute):
            return self.load_attribute(value)
        elif isinstance(value, ast.Subscript):
            return self.load_subscript(value)
        elif isinstance(value, ast.Index):
            return self.load_expr(value.value)
        elif isinstance(value, (ast.Compare, ast.BoolOp)):
            return self.load_extended_bool_op(value)
        elif isinstance(value, ast.BinOp):
            return self.load_bin_op(value)
        elif isinstance(value, ast.UnaryOp):
            return self.load_unary_op(value)
        elif isinstance(value, ast.Call):
            return self.load_call(value)
        else:
            raise NotImplementedError("expression '{}' is not implemented yet".format(value))

    def load_list(self, value):
        loaded_items = [self.load_expr(item) for item in value.elts]
        item_type = self.type_of_objects(loaded_items)
        if item_type is None:
            raise TypeError('list items must be of the same type')

        capacity = len(value.elts)
        item_slots = self.scope.allocate_many(item_type, capacity)
        for dest_slot, item in zip(item_slots, loaded_items):
            self.append_to_body(Assign(dest_slot, item))
        first_item = item_slots[0]
        return Const(first_item.number, ListPointer.of_type(capacity, item_type))

    def type_of_objects(self, objects):
        type_set = set()
        for obj in objects:
            type_set.add(obj.type)
            if len(type_set) > 1:
                return
        if type_set:
            return next(iter(type_set))

    def load_attribute(self, value):
        value_slot = self.load_expr(value.value)
        if issubclass(value_slot.type, GameObjectRef):
            return self.load_game_obj_attr(value_slot, value.attr)
        else:
            raise NotImplementedError("getting attribute of object of type '{}' is not implemented yet".format(value.slot.type))

    def load_game_obj_attr(self, slot, attr_name):
        game_obj_type = slot.type.type
        register = game_obj_type.metadata['abbrev']
        attrib = getattr(game_obj_type, attr_name)
        if slot.is_variable():
            ref = slot
            if slot.ref is not None:
                ref = slot.ref
            slot = attr.assoc(slot, register=register, ref=ref)

        metadata_stub = {**attrib.metadata}
        attrib_type = metadata_stub.pop('type')
        attrib_abbrev = metadata_stub.pop('abbrev')
        metadata = {**slot.metadata, **metadata_stub}

        return attr.assoc(slot, type=attrib_type, attrib=attrib_abbrev,
                          metadata=metadata)

    def load_subscript(self, value):
        value_slot = self.load_expr(value.value)
        slice_slot = self.load_expr(value.slice)
        if isinstance(value_slot, GameObjectList):
            return self.load_game_obj_list_subscript(value_slot, slice_slot)
        elif issubclass(value_slot.type, ListPointer):
            return self.load_list_subscript(value_slot, slice_slot)
        else:
            raise NotImplementedError("getting item of collection of type '{}' is not implemented yet".format(value_slot.type))

    def load_game_obj_list_subscript(self, value_slot, slice_slot):
        register = value_slot.type.metadata['abbrev']
        slot_type = GameObjectRef.of_type(value_slot.type)
        if isinstance(slice_slot, Const):
            return Slot(register, slice_slot.value, None, slot_type)
        else:
            return attr.assoc(slice_slot, type=slot_type)

    def load_list_subscript(self, value_slot, slice_slot, pointer_math_slot=None):
        # TODO: Optimize constant list subscription with constant index
        if isinstance(slice_slot, Const) and slice_slot.value >= value_slot.type.capacity:
            raise IndexError('list index out of range')
        if pointer_math_slot is None:
            pointer_math_slot = self.scope.get_temporary(ListPointer)
        addition = BinOp(value_slot, ast.Add(), slice_slot)
        self.append_to_body(Assign(pointer_math_slot, addition))
        slot = attr.assoc(pointer_math_slot, type=value_slot.type.item_type, ref=pointer_math_slot)
        if issubclass(value_slot.type.item_type, GameObjectRef):
            self.append_to_body(Assign(pointer_math_slot, slot))
        return slot

    def load_extended_bool_op(self, value):
        initial = Const(False, bool)
        if isinstance(value, ast.Compare):
            expr = self.load_compare(value)
        elif isinstance(value, ast.BoolOp):
            # TODO: AND must return last value, OR must return first
            expr = self.load_bool_op(value)
            if isinstance(expr.op, ast.Or):
                initial = Const(True, bool)
                expr.op = ast.And()
                expr.values = list(map(self.negate_bool, expr.values))

        bool_slot = self.scope.get_temporary(bool)
        self.append_to_body(Assign(bool_slot, initial))
        body = [Assign(bool_slot, self.negate_bool(initial))]
        for stmt in self.prepare_if_stmt(expr, body):
            self.append_to_body(stmt)
        return bool_slot

    def load_compare(self, value):
        # TODO: Try to evaluate comparisons literally, e.g. 'x = 3 < 5' -> p1z 1
        left = self.load_expr(value.left)
        comparators = [self.load_expr(comparator) for comparator in value.comparators]

        values = []
        for op, comparator in zip(value.ops, comparators):
            values.append(Compare(left, op, comparator))
            left = comparator
        if len(values) == 1:
            return values[0]
        else:
            return BoolOp(ast.And(), values)

    def load_bool_op(self, value):
        # TODO: Try to evaluate bool operations literally, e.g. 'y = x and False' -> 'p1z 0'
        values = []
        for bool_op_value in value.values:
            if isinstance(bool_op_value, ast.Compare):
                compare = self.load_compare(bool_op_value)
            else:
                slot = self.load_expr(bool_op_value)
                compare = Compare(slot, ast.NotEq(), Const(False, bool))
            values.append(compare)
        return BoolOp(value.op, values)

    def negate_bool(self, expr):
        if isinstance(expr, Const):
            return attr.assoc(expr, value=(not expr.value))
        elif isinstance(expr, Compare):
            op = expr.op
            if isinstance(op, ast.Eq):
                return attr.assoc(expr, op=ast.NotEq())
            elif isinstance(op, ast.NotEq):
                return attr.assoc(expr, op=ast.Eq())
            elif isinstance(op, ast.Lt):
                return attr.assoc(expr, op=ast.GtE())
            elif isinstance(op, ast.LtE):
                return attr.assoc(expr, op=ast.Gt())
            elif isinstance(op, ast.Gt):
                return attr.assoc(expr, op=ast.LtE())
            elif isinstance(op, ast.GtE):
                return attr.assoc(expr, op=ast.Lt())
        else:
            raise NotImplementedError("cannot invert expression '{}'".format(expr))

    def load_bin_op(self, value):
        # TODO: Try to evaluate binary operations literally
        # TODO: Initialize lists, e.g. 'x = [0] * 3'
        left = self.load_expr(value.left)
        right = self.load_expr(value.right)
        return BinOp(left, value.op, right)

    def load_unary_op(self, value):
        operand = self.load_expr(value.operand)
        if isinstance(value.op, ast.UAdd):
            return operand
        elif isinstance(value.op, ast.USub):
            if isinstance(operand, Const):
                return attr.assoc(operand, value=-operand.value)
            else:
                return BinOp(operand, ast.Mult(), Const(-1, Number))
        elif isinstance(value.op, ast.Invert):
            if isinstance(operand, Const):
                return attr.assoc(operand, value=~operand.value)
            else:
                return BinOp(BinOp(operand, ast.Mult(), Const(-1, Number)), ast.Sub(), Const(1, Number))
        else:
            raise NotImplementedError("unary operation '{}' is not implemented yet".format(value.op))

    def load_call(self, value):
        func = self.load_expr(value.func)
        args = [self.load_expr(arg) for arg in value.args]
        if isinstance(func, Slot):
            func_type = func.type
            if issubclass(func_type, GameObjectMethod):
                if value.keywords:
                    raise NotImplementedError('function keywords are not implemented yet')
                func_type.signature.bind(None, *args)
                return Call(func, args)
        elif isinstance(func, BuiltinFunction):
            # kwargs = {kw.arg: self.load_expr(kw.value) for kw in value.keywords}
            if func.func is Range:
                return self.load_range(args)
        else:
            raise NotImplementedError("calling function '{}' is not implemented yet".format(func))

    def load_range(self, args):
        r = Range(*args)
        if isinstance(self.current_node, ast.For):
            return Const(r, type=Range)
        else:
            return self.load_list(ast.List(elts=list(r)))

    def store_value(self, target, src_slot):
        if isinstance(target, ast.Name):
            if self.is_const(target):
                # TODO: Constant must not be redefined
                self.scope.define_const(target.id, src_slot)
            else:
                return self.scope.assign(target.id, src_slot)
        elif isinstance(target, ast.Attribute):
            return self.load_attribute(target)
        elif isinstance(target, ast.Subscript):
            return self.load_subscript(target)
        else:
            raise NotImplementedError("assigning values to '{}' is not implemented yet".format(target))

    def is_const(self, target):
        return target.id is not None and target.id.isupper()

    def is_black_hole(self, target):
        return isinstance(target, ast.Name) and target.id == '_'

    def append_to_body(self, stmt):
        body = self.bodies[-1]
        body.append(stmt)


@attr.s
class Scope:
    names = attr.ib(default=attr.Factory(dict))
    numeric_slots = attr.ib(default=attr.Factory(lambda: Slots(start=1)))
    string_slots = attr.ib(default=attr.Factory(lambda: Slots()))
    temporary_slots = attr.ib(default=attr.Factory(list))
    recycled_temporary_slots = attr.ib(default=attr.Factory(lambda: defaultdict(list)))

    def __attrs_post_init__(self):
        self.populate_builtins()
        self.populate_game_objects()

    def populate_builtins(self):
        self.names['range'] = BuiltinFunction(Range)

    def populate_game_objects(self):
        self.names['yegiks'] = GameObjectList(Yegik)
        self.names['points'] = GameObjectList(Point)
        self.names['bots'] = GameObjectList(Bot)
        self.names['timers'] = GameObjectList(Timer)
        self.names['system'] = Slot(System.metadata['abbrev'], None, None, GameObjectRef.of_type(System))

    def define_const(self, name, value):
        self.names[name] = value

    def assign(self, name, src_slot):
        slot = self.names.get(name)
        if slot is not None:
            # TODO: Check destination type
            return slot
        slot = self.allocate(src_slot.type)
        slot.type = src_slot.type
        slot.metadata = src_slot.metadata
        self.names[name] = slot
        return slot

    def allocate(self, type):
        if issubclass(type, Number):
            number = self.numeric_slots.allocate()
            return Slot('p', number, 'z', Number)
        elif issubclass(type, str):
            number = self.string_slots.allocate()
            return Slot('s', number, 'z', str)
        else:
            raise TypeError("cannot allocate slot of type '{}'".format(type))

    def allocate_many(self, type, length):
        return [self.allocate(type) for _ in range(length)]

    def get_temporary(self, type):
        if not issubclass(type, (Number, str)):
            raise TypeError("cannot create volatile slot of type '{}'".format(type))
        if self.recycled_temporary_slots[type]:
            return self.recycled_temporary_slots[type].pop()
        slot = Slot('p', None, 'z', type)
        self.temporary_slots.append(slot)
        return slot

    def recycle_temporary(self, slot):
        self.recycled_temporary_slots[slot.type].append(slot)

    def allocate_temporary(self):
        for slot in self.temporary_slots:
            new_slot = self.allocate(slot.type)
            slot.number = new_slot.number

    def get(self, name):
        slot = self.names.get(name)
        if slot is None:
            raise NameError("name '{}' is not defined".format(name))
        return slot


@attr.s
class Slots:
    start = attr.ib(default=0)
    stop = attr.ib(default=100)
    slots = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.slots = [None for x in range(self.start, self.stop)]

    def allocate(self):
        for addr, value in enumerate(self.slots):
            if value is None:
                self.slots[addr] = RESERVED
                return addr + self.start
        raise MemoryError('ran out of variable slots')

    def free(self, addr):
        self.slots[addr-self.start] = None

    def count_reserved(self):
        return len(slot for slot in self.slots if slot is RESERVED)


RESERVED = object()
