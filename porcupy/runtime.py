import attr

from .types import IntType, FloatType, BoolType, StringType, GameObjectRef, GameObjectMethod

int_type = IntType()
bool_type = BoolType()
float_type = FloatType()
str_type = StringType()

# TODO: Add more game objects


@attr.s
class Timer:
    value = attr.ib(default=0, metadata={'abbrev': 'i', 'type': int_type})
    enabled = attr.ib(default=0, metadata={'abbrev': 'r', 'type': bool_type})

    metadata = {'abbrev': 't'}

    def start(self) -> None:
        pass

    start.metadata = {'abbrev': 'g', 'type': GameObjectMethod(start)}

    def stop(self) -> None:
        pass

    stop.metadata = {'abbrev': 's', 'type': GameObjectMethod(stop)}


@attr.s
class System:
    bots = attr.ib(default=0, metadata={'abbrev': 'b', 'type': int_type})
    color = attr.ib(default=0, metadata={'abbrev': 'c', 'type': int_type})

    metadata = {'abbrev': 'y'}

    def print(self, s: str_type) -> None:
        pass

    print.metadata = {'abbrev': 'm', 'type': GameObjectMethod(print)}

    def print_at(self, x: float_type, y: float_type, dur: float_type, s: str_type) -> None:
        pass

    print_at.metadata = {'abbrev': 'y', 'type': GameObjectMethod(print_at)}

    # def set_color(self, r: int_type, g: int_type, b: int_type) -> None:
    #     self.color = r + (g << 8) + (b << 16)

    # set_color.metadata = {'type': GameObjectMethod(set_color)}

    def load_map(self, name: str_type) -> None:
        pass

    load_map.metadata = {'abbrev': 'l', 'type': GameObjectMethod(load_map)}


@attr.s
class Point:
    pos_x = attr.ib(default=0.0, metadata={'abbrev': 'x', 'type': float_type})
    pos_y = attr.ib(default=0.0, metadata={'abbrev': 'y', 'type': float_type})

    metadata = {'abbrev': 'c'}


@attr.s
class Bot:
    ai = attr.ib(default=False, metadata={'abbrev': 'i', 'type': bool_type})
    target = attr.ib(default=0, metadata={'abbrev': 't', 'type': int_type})
    level = attr.ib(default=0, metadata={'abbrev': 'l', 'type': int_type})
    point = attr.ib(default=0, metadata={'abbrev': 'p', 'type': int_type})
    goto = attr.ib(default=0, metadata={'abbrev': 'g', 'type': GameObjectRef(Point)})

    metadata = {'abbrev': 'a'}


@attr.s
class Yozhik:
    frags = attr.ib(default=0, metadata={'abbrev': 'f', 'type': int_type})
    pos_x = attr.ib(default=0.0, metadata={'abbrev': 'x', 'type': float_type})
    pos_y = attr.ib(default=0.0, metadata={'abbrev': 'y', 'type': float_type})
    speed_x = attr.ib(default=0.0, metadata={'abbrev': 'u', 'type': float_type})
    speed_y = attr.ib(default=0.0, metadata={'abbrev': 'v', 'type': float_type})
    health = attr.ib(default=0, metadata={'abbrev': 'p', 'type': int_type})
    armor = attr.ib(default=0, metadata={'abbrev': 'n', 'type': int_type})
    has_weapon = attr.ib(default=False, metadata={'abbrev': 'e', 'type': bool_type})
    weapon = attr.ib(default=0, metadata={'abbrev': 'w', 'type': int_type})
    ammo = attr.ib(default=0, metadata={'abbrev': 's', 'type': int_type})
    view_angle = attr.ib(default=0.0, metadata={'abbrev': 'a', 'type': float_type})

    metadata = {'abbrev': 'e'}

    def spawn(self, point: int_type) -> None:
        pass

    spawn.metadata = {'abbrev': 'b', 'type': GameObjectMethod(spawn)}