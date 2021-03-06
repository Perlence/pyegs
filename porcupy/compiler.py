import ast
from collections import defaultdict
from fractions import Fraction

import attr

from .ast import (AST, Module, Assign, If, Const, Slot, EvolvedSlot, BoolOp,
                  operator, Add, Sub, Mult, Div, FloorDiv, Mod, Compare, Label,
                  Call)
from .gameobjs import (Yozhik, Timer, Point, Bot, System, Button, Door,
                       Viewport, Sheep)
from .types import (NumberType, IntType, BoolType, FloatType, StringType,
                    ListPointer, Slice, CallableType, check_type)


def compile(source, filename='<unknown>', separate_stmts=False):
    ast_tree = ast.parse(source, filename)

    converter = NodeConverter()
    converted_tree = visit_with_exc_wrapping(converter, ast_tree, filename)
    if converted_tree is None:
        return
    converter.scope.allocate_temporary()
    compiled = str(converted_tree)
    if not separate_stmts:
        compiled = ' '.join(compiled.split('  '))
    return compiled


def visit_with_exc_wrapping(converter, node, filename):
    try:
        return converter.visit(node)
    except Exception as exc:
        node = converter.current_stmt
        if node is None:
            raise

        lineno = getattr(node, 'lineno', None)
        if lineno is None:
            raise

        exc._porcupy_lineno = lineno
        raise


@attr.s
class NodeConverter:
    scope = attr.ib(default=attr.Factory(lambda: Scope()))
    body = attr.ib(default=attr.Factory(list))
    last_label = attr.ib(default=0)
    loop_labels = attr.ib(default=attr.Factory(list))
    current_stmt = attr.ib(default=None)
    slots_to_recycle_later = attr.ib(default=attr.Factory(lambda: defaultdict(list)))

    def visit(self, node):
        if isinstance(node, AST):
            return node
        if isinstance(node, ast.stmt):
            self.current_stmt = node

        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        result = visitor(node)

        for slot in self.slots_to_recycle_later[node]:
            self.scope.recycle_temporary(slot)
        del self.slots_to_recycle_later[node]

        return result

    def generic_visit(self, node):
        raise NotImplementedError("node '{}' is not implemented".format(node))

    def visit_Module(self, node):
        for stmt in node.body:
            self.visit(stmt)
        return Module(self.body)

    def visit_Assign(self, node):
        # TODO: Reassign lists to list pointers without allocating more memory:
        # 'x = [11, 22]; x = [11, 22]' -> 'p1z 11 p2z 22 p4z 1 p1z 11 p2z 22'
        src_slots = {}
        for target in node.targets:
            if isinstance(target, ast.List):
                raise NotImplementedError('list unpacking is not supported')

            targets, values = self.unpack_tuples(target, node.value)
            dest_src_slots = []
            for target, value in zip(targets, values):
                if self.is_black_hole(target):
                    continue

                if isinstance(value, AST):
                    src_slot = value
                else:
                    src_slot = src_slots.get(value)
                    if src_slot is None:
                        src_slot = src_slots[value] = self.visit(value)

                dest_slot = self.store_value(target, src_slot)
                if dest_slot is None:
                    continue

                if len(values) > 1 and not isinstance(src_slot, Const):
                    temp = self.scope.get_temporary(src_slot.type)
                    self.recycle_later(temp)
                    self.append_assign(temp, src_slot)
                    src_slot = temp

                dest_src_slots.append((dest_slot, src_slot))

            for dest_slot, src_slot in dest_src_slots:
                self.append_assign(dest_slot, src_slot)

    def unpack_tuples(self, target, value):
        targets = target.elts if isinstance(target, ast.Tuple) else [target]
        values = value.elts if isinstance(value, ast.Tuple) else [value]
        if len(targets) < len(values):
            raise ValueError('too many values to unpack (expected {})'.format(len(targets)))
        elif len(targets) > len(values):
            raise ValueError('not enough values to unpack (expected {}, got {})'.format(len(targets), len(values)))
        return targets, values

    def is_black_hole(self, target):
        return isinstance(target, ast.Name) and target.id == '_'

    def visit_AugAssign(self, node):
        src_slot = self.visit(node.value)
        dest_slot = self.visit(node.target)
        bin_op = self.visit(ast.BinOp(dest_slot, node.op, src_slot))
        self.append_assign(dest_slot, bin_op)

    def store_value(self, target, src_slot):
        dest_slot = None
        if isinstance(target, AST):
            dest_slot = target
        elif isinstance(target, ast.Name):
            if self.is_target_const(target):
                if not self.is_source_const(src_slot):
                    raise TypeError("cannot define a constant '{}' with value '{}'".format(target.id, src_slot))
                elif isinstance(src_slot.type, Slice):
                    raise TypeError('slice cannot be constant')
                elif target.id in self.scope.names:
                    raise ValueError("cannot redefine a constant '{}'".format(target.id))
                self.scope.define_const(target.id, src_slot)
            else:
                dest_slot = self.scope.assign(target.id, src_slot)
        elif isinstance(target, (ast.Attribute, ast.Subscript)):
            dest_slot = self.visit(target)
        else:
            raise NotImplementedError("assigning values to '{}' is not implemented".format(target))

        if dest_slot is None:
            return
        elif dest_slot.metadata.get('readonly'):
            raise TypeError("cannot assign value to a read-only slot '{}'".format(dest_slot))
        return dest_slot

    def is_source_const(self, src_slot):
        return (isinstance(src_slot, Const) or
                isinstance(src_slot, (Slot, EvolvedSlot)) and not src_slot.is_variable())

    def visit_For(self, node):
        # For(expr target, expr iter, stmt* body, stmt* orelse)
        if self.is_body_empty(node.body) and self.is_body_empty(node.orelse):
            return

        temp_index = None
        if isinstance(node.target, ast.Tuple):
            if len(node.target.elts) != 2:
                raise ValueError('exactly 2 receiver variables required, got {}'.format(len(node.target.elts)))
            index, target = node.target.elts
            index = self.store_value(index, Const(-1))
        else:
            index = temp_index = self.scope.get_temporary(IntType())
            target = node.target

        self.append_assign(index, Const(-1))

        iter_slot = self.visit(node.iter)
        iter_len = iter_slot.type._len(self, iter_slot)

        test = Compare(index, ast.Lt(), iter_len)

        subscript = ast.Subscript(value=iter_slot, slice=index, ctx=ast.Load())
        assign = ast.Assign(targets=[target], value=subscript)
        body = [assign] + node.body

        increment_index = ast.AugAssign(target=index, op=ast.Add(), value=Const(1))

        self.visit_While(ast.While(test, body, node.orelse), before_test=increment_index)

        if temp_index is not None:
            self.scope.recycle_temporary(temp_index)

    def visit_While(self, node, before_test=None):
        # While(expr test, stmt* body, stmt* orelse)
        label_start = self.new_label()
        self.append_node(label_start)
        if before_test is not None:
            self.visit(before_test)
        self.generic_if(node, label_start)
        self.loop_labels.pop()

    def visit_If(self, node):
        # If(expr test, stmt* body, stmt* orelse)
        if self.is_body_empty(node.body) and self.is_body_empty(node.orelse):
            return
        if (isinstance(node.test, (ast.Num, ast.Str, ast.NameConstant, ast.List)) or
                isinstance(node.test, ast.Name) and self.is_target_const(node.test)):
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

    def generic_if(self, node, label_start=None):
        is_loop = label_start is not None

        if isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1:
            test = self.visit_Compare(node.test, wrap_in_if_stmt=False)
        else:
            test = self.visit(node.test)

        if not isinstance(test, Compare):
            test = Compare(test.type._truthy(self, test), ast.NotEq(), Const(False))

        not_test = self.negate_bool(test)

        label_end = self.new_label()
        goto_end = Slot('g', label_end.index, 'z', None)
        if is_loop:
            self.loop_labels.append((label_start, label_end))

        label_else = None
        if not self.is_body_empty(node.orelse):
            label_else = self.new_label()
            goto_else = Slot('g', label_else.index, 'z', None)
            self.append_node(If(not_test, [goto_else]))
        else:
            self.append_node(If(not_test, [goto_end]))

        for stmt in node.body:
            self.visit(stmt)

        if is_loop:
            goto_start = Slot('g', label_start.index, 'z', None)
            self.append_node(goto_start)

        if label_else is not None:
            if not is_loop:
                self.append_node(goto_end)
            self.append_node(label_else)

            for stmt in node.orelse:
                self.visit(stmt)

        self.append_node(label_end)

    def new_label(self):
        self.last_label += 1
        if self.last_label > 99:
            raise ValueError('ran out of jump labels')
        return Label(self.last_label)

    def visit_Continue(self, node):
        label_start, _ = self.loop_labels[-1]
        goto_start = Slot('g', label_start.index, 'z', None)
        self.append_node(goto_start)

    def visit_Break(self, node):
        _, label_end = self.loop_labels[-1]
        goto_end = Slot('g', label_end.index, 'z', None)
        self.append_node(goto_end)

    def visit_Pass(self, node):
        pass

    def visit_Expr(self, node):
        if not isinstance(node.value, ast.Call):
            return
        expr = self.visit_Call(node.value, raise_if_returns=True)
        if expr is not None:
            self.append_node(expr)

    def visit_Num(self, node):
        if not isinstance(node.n, float):
            return Const(node.n)
        frac = Fraction(str(node.n))
        if frac.denominator == 1:
            return Const(frac.numerator, FloatType())
        return self.visit(ast.BinOp(Const(frac.numerator), ast.Div(), Const(frac.denominator)))

    def visit_Str(self, node):
        return Const(node.s)

    def visit_NameConstant(self, node):
        return Const(node.value)

    def visit_List(self, node):
        if not node.elts:
            raise ValueError('cannot allocate an empty list')

        loaded_items = [self.visit(item) for item in node.elts]
        item_type = self.type_of_objects(loaded_items)
        if item_type is None:
            raise TypeError('list items must be of the same type')

        capacity = len(node.elts)
        item_slots = self.scope.allocate_many(item_type, capacity)
        for dest_slot, item in zip(item_slots, loaded_items):
            self.append_assign(dest_slot, item)
        first_item = item_slots[0]
        return Const(first_item.index, ListPointer(item_type, capacity))

    def type_of_objects(self, objects):
        type_set = set()
        for obj in objects:
            type_set.add(obj.type)
            if len(type_set) > 1:
                return
        if type_set:
            return next(iter(type_set))

    def visit_Name(self, node):
        return self.scope.get(node.id)

    def visit_Attribute(self, node):
        value_slot = self.visit(node.value)
        if not hasattr(value_slot.type, '_getattr'):
            raise NotImplementedError("getting attribute of object of type '{}' is not implemented"
                                      .format(value_slot.type))
        return value_slot.type._getattr(self, value_slot, node.attr)

    def visit_Subscript(self, node):
        value_slot = self.visit(node.value)
        slice_slot = self.visit(node.slice)
        if isinstance(slice_slot, slice):
            return self.load_slice_subscript(value_slot, slice_slot)
        else:
            return self.load_index_subscript(value_slot, slice_slot, node.ctx)

    def load_index_subscript(self, value_slot, slice_slot, ctx):
        if isinstance(ctx, ast.Load):
            if not hasattr(value_slot.type, '_getitem'):
                raise NotImplementedError("getting item of collection of type '{}' is not implemented"
                                          .format(value_slot.type))
            return value_slot.type._getitem(self, value_slot, slice_slot)

        elif isinstance(ctx, ast.Store):
            if not hasattr(value_slot.type, '_setitem'):
                raise NotImplementedError("setting item of collection of type '{}' is not implemented"
                                          .format(value_slot.type))
            return value_slot.type._setitem(self, value_slot, slice_slot)

    def load_slice_subscript(self, value_slot, slice_slot):
        list_ptr = value_slot.type._getptr(self, value_slot)
        src_capacity = value_slot.type._cap(self, value_slot)
        src_length = value_slot.type._len(self, value_slot)

        lower = slice_slot.start
        upper = slice_slot.stop

        if lower is None:
            lower = Const(0)
        if upper is None:
            upper = src_length

        ptr_value = self.visit(ast.BinOp(list_ptr, ast.Add(), lower))
        len_value = self.visit(ast.BinOp(upper, ast.Sub(), lower))
        cap_value = self.visit(ast.BinOp(src_capacity, ast.Sub(), lower))

        slice_type = Slice(value_slot.type.item_type)
        slice_value = slice_type._new(self, ptr_value, len_value, cap_value)

        return slice_value

    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_Slice(self, node):
        lower, upper = None, None
        if node.step is not None:
            raise NotImplementedError('slice step is not supported')
        if node.lower is not None:
            lower = self.visit(node.lower)
        if node.upper is not None:
            upper = self.visit(node.upper)
        return slice(lower, upper)

    def visit_Compare(self, node, initial=False, wrap_in_if_stmt=True):
        # TODO: Try to evaluate comparisons literally, e.g. 'x = 3 < 5' -> p1z 1
        initial = Const(initial)
        left = self.visit(node.left)
        comparators = [self.visit(comparator) for comparator in node.comparators]

        values = []
        for op, comparator in zip(node.ops, comparators):
            values.append(Compare(left, op, comparator))
            left = comparator

        if len(values) == 1:
            expr = values[0]
        else:
            expr = BoolOp(ast.And(), values)

        if wrap_in_if_stmt:
            return self.wrap_in_if_stmt(expr, initial)
        else:
            return expr

    def visit_BoolOp(self, node, initial=False):
        # TODO: Try to evaluate bool operations literally, e.g. 'y = x and False' -> 'p1z 0'
        initial = Const(initial)
        const_false = Const(False)
        values = []
        for bool_op_value in node.values:
            if isinstance(bool_op_value, ast.Compare):
                value = self.visit_Compare(bool_op_value, wrap_in_if_stmt=False)
                if isinstance(value, BoolOp):
                    bool_slot = self.wrap_in_if_stmt(value, initial=const_false)
                    value = Compare(bool_slot.type._truthy(self, bool_slot), ast.NotEq(), Const(False))
            else:
                slot = self.visit(bool_op_value)
                value = Compare(slot.type._truthy(self, slot), ast.NotEq(), Const(False))
            values.append(value)

        op = node.op
        if isinstance(node.op, ast.Or):
            initial = self.negate_bool(initial)
            op = ast.And()
            values = list(map(self.negate_bool, values))

        expr = BoolOp(op, values)

        return self.wrap_in_if_stmt(expr, initial)

    def wrap_in_if_stmt(self, expr, initial):
        bool_slot = self.scope.get_temporary(BoolType())
        self.append_assign(bool_slot, initial)
        assign = Assign(bool_slot, self.negate_bool(initial))
        self.append_node(If(expr, [assign]))
        self.recycle_later(bool_slot)
        return bool_slot

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        op = self.convert_bin_operator(node.op)
        right = self.visit(node.right)
        return left.type._bin_op(self, left, op, right)

    def convert_bin_operator(self, value):
        if isinstance(value, operator):
            return value
        if not isinstance(value, ast.operator):
            raise SyntaxError("value '{}' is not a binary operator".format(value))

        if isinstance(value, ast.Add):
            return Add()
        elif isinstance(value, ast.Sub):
            return Sub()
        elif isinstance(value, ast.Mult):
            return Mult()
        elif isinstance(value, ast.Div):
            return Div()
        elif isinstance(value, ast.FloorDiv):
            return FloorDiv()
        elif isinstance(value, ast.Mod):
            return Mod()
        else:
            raise NotImplementedError("operation '{}' is not implemented".format(value))

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            if isinstance(node.operand, ast.Compare):
                return self.visit_Compare(node.operand, initial=True)
            elif isinstance(node.operand, ast.BoolOp):
                return self.visit_BoolOp(node.operand, initial=True)

        operand = self.visit(node.operand)
        return operand.type._unary_op(self, node.op, operand)

    def visit_Call(self, node, raise_if_returns=False):
        if node.keywords:
            raise NotImplementedError('function keywords are not implemented')
        func = self.visit(node.func)
        args = [self.visit(arg) for arg in node.args]
        if not hasattr(func.type, '_call'):
            raise NotImplementedError("calling function '{}' is not implemented".format(func))
        result = func.type._call(self, func, *args)

        if raise_if_returns:
            if result is not None and not isinstance(result, Call):
                raise ValueError('function return value is unused')

        return result

    def visit_IfExp(self, node):
        body = self.visit(node.body)
        tmp = self.scope.get_temporary(body.type)
        self.visit(ast.If(node.test, [
            ast.Assign([tmp], body)
        ], [
            ast.Assign([tmp], node.orelse)
        ]))
        self.recycle_later(tmp)
        return tmp

    def negate_bool(self, expr):
        if isinstance(expr, Const):
            return attr.evolve(expr, value=(not expr.value))
        elif isinstance(expr, Compare):
            op = expr.op
            if isinstance(op, ast.Eq):
                return attr.evolve(expr, op=ast.NotEq())
            elif isinstance(op, ast.NotEq):
                return attr.evolve(expr, op=ast.Eq())
            elif isinstance(op, ast.Lt):
                return attr.evolve(expr, op=ast.GtE())
            elif isinstance(op, ast.LtE):
                return attr.evolve(expr, op=ast.Gt())
            elif isinstance(op, ast.Gt):
                return attr.evolve(expr, op=ast.LtE())
            elif isinstance(op, ast.GtE):
                return attr.evolve(expr, op=ast.Lt())
        else:
            raise NotImplementedError("cannot negate expression '{}'".format(expr))

    def append_assign(self, dest, src):
        check_type(dest, src)
        self.append_node(Assign(dest, src))

    def append_node(self, stmt):
        self.body.append(stmt)

    def recycle_later(self, *slots):
        self.slots_to_recycle_later[self.current_stmt].extend(slots)

    def is_body_empty(self, body):
        return all(isinstance(stmt, ast.Pass) for stmt in body)

    def is_target_const(self, target):
        return target.id is not None and target.id.isupper()


@attr.s
class Scope:
    names = attr.ib(default=attr.Factory(dict))
    numeric_slots = attr.ib(default=attr.Factory(lambda: Slots(start=1)))
    string_slots = attr.ib(default=attr.Factory(lambda: Slots()))
    temporary_slots = attr.ib(default=attr.Factory(list))
    recycled_temporary_slots = attr.ib(default=attr.Factory(lambda: defaultdict(list)))

    def __attrs_post_init__(self):
        self.populate_builtins()
        self.populate_consts()
        self.populate_game_objects()
        self.populate_system_functions()

    def populate_builtins(self):
        from .functions import capacity, length, randint, slice
        from .types import Range, Reversed

        self.names['bool'] = Const(None, BoolType())
        self.names['float'] = Const(None, FloatType())
        self.names['int'] = Const(None, IntType())

        self.names['range'] = Const(None, Range())
        self.names['reversed'] = Const(None, Reversed())

        self.names['cap'] = Const(None, CallableType.from_function(capacity))
        self.names['len'] = Const(None, CallableType.from_function(length))
        self.names['randint'] = Const(None, CallableType.from_function(randint))
        self.names['slice'] = Const(None, CallableType.from_function(slice))

    def populate_game_objects(self):
        from .types import GameObjectList
        self.names['bots'] = Const(None, GameObjectList(Bot(), 1, 10))
        self.names['buttons'] = Const(None, GameObjectList(Button(), 1, 50))
        self.names['doors'] = Const(None, GameObjectList(Door(), 1, 50))
        self.names['points'] = Const(None, GameObjectList(Point(), 1, 100))
        self.names['timers'] = Const(None, GameObjectList(Timer(), 1, 100))
        self.names['yozhiks'] = Const(None, GameObjectList(Yozhik(), 1, 10))
        self.names['system'] = Slot(System.metadata['abbrev'], None, None, System())
        self.names['viewport'] = Slot(Viewport.metadata['abbrev'], None, None, Viewport())

    def populate_consts(self):
        from .gameobjs import Weapon, FragLimit, GameMode, DoorState, BotLevel
        int_type = IntType()

        self.names['W_BFG10K'] = Const(int(Weapon.bfg10k), int_type)
        self.names['W_BLASTER'] = Const(int(Weapon.blaster), int_type)
        self.names['W_SHOTGUN'] = Const(int(Weapon.shotgun), int_type)
        self.names['W_SUPER_SHOTGUN'] = Const(int(Weapon.super_shotgun), int_type)
        self.names['W_MACHINE_GUN'] = Const(int(Weapon.machine_gun), int_type)
        self.names['W_CHAIN_GUN'] = Const(int(Weapon.chain_gun), int_type)
        self.names['W_GRENADE_LAUNCHER'] = Const(int(Weapon.grenade_launcher), int_type)
        self.names['W_ROCKET_LAUNCHER'] = Const(int(Weapon.rocket_launcher), int_type)
        self.names['W_HYPERBLASTER'] = Const(int(Weapon.hyperblaster), int_type)
        self.names['W_RAILGUN'] = Const(int(Weapon.railgun), int_type)

        self.names['DS_CLOSED'] = Const(int(DoorState.closed), int_type)
        self.names['DS_OPEN'] = Const(int(DoorState.open), int_type)
        self.names['DS_OPENING'] = Const(int(DoorState.opening), int_type)
        self.names['DS_CLOSING'] = Const(int(DoorState.closing), int_type)

        self.names['FL_10'] = Const(int(FragLimit.ten), int_type)
        self.names['FL_20'] = Const(int(FragLimit.twenty), int_type)
        self.names['FL_30'] = Const(int(FragLimit.thirty), int_type)
        self.names['FL_50'] = Const(int(FragLimit.fifty), int_type)
        self.names['FL_100'] = Const(int(FragLimit.one_hundred), int_type)
        self.names['FL_200'] = Const(int(FragLimit.two_hundred), int_type)

        self.names['BL_VERY_EASY'] = Const(int(BotLevel.very_easy), int_type)
        self.names['BL_EASY'] = Const(int(BotLevel.easy), int_type)
        self.names['BL_NORMAL'] = Const(int(BotLevel.normal), int_type)
        self.names['BL_HARD'] = Const(int(BotLevel.hard), int_type)
        self.names['BL_IMPOSSIBLE'] = Const(int(BotLevel.impossible), int_type)

        self.names['GM_MULTI_LAN'] = Const(int(GameMode.multi_lan), int_type)
        self.names['GM_MULTI_DUEL'] = Const(int(GameMode.multi_duel), int_type)
        self.names['GM_HOT_SEAT'] = Const(int(GameMode.hot_seat), int_type)
        self.names['GM_MENU'] = Const(int(GameMode.menu), int_type)
        self.names['GM_SINGLE'] = Const(int(GameMode.single), int_type)
        self.names['GM_SHEEP'] = Const(int(GameMode.sheep), int_type)
        self.names['GM_HOT_SEAT_SPLIT'] = Const(int(GameMode.hot_seat_split), int_type)

    def populate_system_functions(self):
        from .types import GameObjectMethod

        system = System()
        sheep = Sheep()
        methods = [
            (system, 'print', 'print'),
            (system, 'print_at', 'print_at'),
            (system, 'set_color', 'set_color'),
            (system, 'load_map', 'load_map'),
            (sheep, 'spawn', 'spawn_sheep'),
        ]
        for type, attrname, name in methods:
            method = getattr(type, attrname)
            try:
                metadata = method.metadata
                method_abbrev = metadata['abbrev']
            except (AttributeError, KeyError):
                self.names[name] = Const(None, CallableType.from_function(method))
            else:
                self.names[name] = Slot(type.metadata['abbrev'],
                                        None,
                                        method_abbrev,
                                        GameObjectMethod(method))

    def define_const(self, name, value):
        self.names[name] = value

    def assign(self, name, src_slot):
        slot = self.names.get(name)
        if slot is not None:
            return slot

        slot = self.allocate(src_slot.type)
        slot.type = src_slot.type
        slot.metadata = src_slot.metadata
        self.names[name] = slot
        return slot

    def get_by_index(self, index, type):
        if isinstance(type, NumberType):
            slots = self.numeric_slots
            register = 'p'
        elif isinstance(type, StringType):
            slots = self.string_slots
            register = 's'
        else:
            raise TypeError("cannot get slot of type '{}'".format(type))

        if not slots.is_reserved(index):
            raise IndexError("slot #{} is not reserved".format(index))
        return Slot(register, index, 'z', type)

    def allocate(self, type):
        if isinstance(type, NumberType):
            # TODO: Allocate numeric variables on string slots, when all
            # numeric slots are already taken
            index = self.numeric_slots.allocate()
            return Slot('p', index, 'z', type)
        else:
            raise TypeError("cannot allocate slot of type '{}'".format(type))

    def allocate_many(self, type, length):
        return [self.allocate(type) for _ in range(length)]

    def get_temporary(self, type):
        if not isinstance(type, (NumberType, StringType)):
            raise TypeError("cannot create temporary slot of type '{}'".format(type))

        base_type = self.base_type(type)
        if self.recycled_temporary_slots[base_type]:
            slot = self.recycled_temporary_slots[base_type].pop()
            slot.type = type
            return slot

        slot = Slot('p', None, 'z', type)
        self.temporary_slots.append(slot)
        return slot

    def recycle_temporary(self, slot):
        base_type = self.base_type(slot.type)
        self.recycled_temporary_slots[base_type].append(slot)

    def base_type(self, type):
        if isinstance(type, NumberType):
            return NumberType()
        elif isinstance(type, StringType):
            return StringType()

    def allocate_temporary(self):
        for slot in self.temporary_slots:
            new_slot = self.allocate(slot.type)
            slot.index = new_slot.index

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

    def is_reserved(self, addr):
        return self.slots[addr-self.start] is RESERVED


RESERVED = object()
