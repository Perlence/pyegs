import ast
from numbers import Number

import attr

__all__ = ('AST', 'Module', 'Assign', 'If', 'Const', 'Slot', 'BoolOp', 'BinOp', 'Compare', 'Call', 'Label')


class AST:
    pass


@attr.s
class Module(AST):
    body = attr.ib()

    def __str__(self):
        return ' '.join(map(str, self.body))


@attr.s
class Assign(AST):
    target = attr.ib()
    value = attr.ib()

    def __str__(self):
        return ' '.join([str(self.target), str(self.value)])


@attr.s
class If(AST):
    test = attr.ib()
    body = attr.ib()

    def __str__(self):
        body = ' '.join(map(str, self.body))
        return '# {} ( {} )'.format(self.test, body)


@attr.s
class Const(AST):
    value = attr.ib()
    type = attr.ib()
    metadata = attr.ib(default=attr.Factory(dict))

    def __str__(self):
        if issubclass(self.type, bool):
            return str(int(self.value))
        elif issubclass(self.type, Number):
            return str(self.value).replace('.', ',')
        elif issubclass(self.type, str):
            return self.value.replace(' ', '_')
        else:
            raise TypeError("cannot format '{}' const".format(self.type))


@attr.s
class Slot(AST):
    register = attr.ib()
    number = attr.ib()
    attrib = attr.ib()
    type = attr.ib()
    metadata = attr.ib(default=attr.Factory(dict))
    ref = attr.ib(default=None)

    def is_variable(self):
        return self.register in ('p', 's')

    def __str__(self):
        if self.attrib is not None:
            number = self.number
            if self.ref is not None:
                number = self.ref.number
            if number is None:
                number = ''
            return '{register}{ref}{number}{attrib}'.format(
                register=self.register,
                ref='^' if self.ref is not None else '',
                number=number,
                attrib=self.attrib)
        else:
            return str(self.number)


@attr.s
class BoolOp(AST):
    op = attr.ib()
    values = attr.ib()

    type = attr.ib(init=False)
    metadata = attr.ib(default=attr.Factory(dict))

    def __attrs_post_init__(self):
        self.type = bool

    def __str__(self):
        result = []
        first, *rest = self.values
        result.append(str(first))
        for value in rest:
            result.append(self.translate_boolop(self.op))
            result.append(str(value))
        return ' '.join(result)

    def translate_boolop(self, op):
        if not isinstance(op, ast.boolop):
            raise SyntaxError("node '{}' is not a boolean operator".format(op))

        if isinstance(op, ast.And):
            return '&'
        elif isinstance(op, ast.Or):
            return '|'


@attr.s
class BinOp(AST):
    left = attr.ib()
    op = attr.ib()
    right = attr.ib()

    type = attr.ib(init=False)
    metadata = attr.ib(default=attr.Factory(dict))

    def __attrs_post_init__(self):
        if issubclass(self.left.type, self.right.type):
            self.type = self.right.type
        elif issubclass(self.right.type, self.left.type):
            self.type = self.left.type
        else:
            raise TypeError("operands '{}' and '{}' are not of the same type".format(self.left, self.right))

    def __str__(self):
        return '{}{}{}'.format(self.left, self.translate_operator(self.op), self.right)

    def translate_operator(self, op):
        if not isinstance(op, ast.operator):
            raise SyntaxError("node '{}' is not a binary operator".format(op))

        if isinstance(op, ast.Add):
            return '+'
        elif isinstance(op, ast.Sub):
            return '-'
        elif isinstance(op, ast.Mult):
            return '*'
        elif isinstance(op, ast.Div):
            return '/'
        elif isinstance(op, ast.FloorDiv):
            return '{'
        elif isinstance(op, ast.Mod):
            return '}'
        else:
            raise NotImplementedError("operation '{}' is not implemented yet".format(op))


@attr.s
class Compare(AST):
    left = attr.ib()
    op = attr.ib()
    right = attr.ib()

    type = attr.ib(init=False)
    metadata = attr.ib(default=attr.Factory(dict))

    def __attrs_post_init__(self):
        self.type = bool

    def __str__(self):
        return '{} {} {}'.format(self.left, self.translate_cmpop(self.op), self.right)

    def translate_cmpop(self, op):
        if not isinstance(op, ast.cmpop):
            raise SyntaxError("node '{}' is not a comparison operator".format(op))

        if isinstance(op, ast.Eq):
            return '='
        elif isinstance(op, ast.NotEq):
            return '!'
        elif isinstance(op, ast.Lt):
            return '<'
        elif isinstance(op, ast.LtE):
            return '<='
        elif isinstance(op, ast.Gt):
            return '>'
        elif isinstance(op, ast.GtE):
            return '>='


@attr.s
class Call(AST):
    func = attr.ib()
    args = attr.ib()

    def __str__(self):
        result = [str(self.func)]
        if self.args:
            positional_args = ' '.join(map(str, self.args))
            result.append(positional_args)
        return ' '.join(result)


@attr.s
class Label(AST):
    number = attr.ib()

    def __str__(self):
        return ':{}'.format(self.number)