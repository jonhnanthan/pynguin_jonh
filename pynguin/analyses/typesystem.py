#  This file is part of Pynguin.
#
#  SPDX-FileCopyrightText: 2019–2022 Pynguin Contributors
#
#  SPDX-License-Identifier: LGPL-3.0-or-later
#
"""Provides analyses for a module's type information."""
from __future__ import annotations

import functools
import inspect
import logging
import types
import typing
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import _BaseGenericAlias  # type:ignore
from typing import Any, Final, Generic, TypeVar, cast, get_origin, get_type_hints

import networkx as nx
from networkx.drawing.nx_pydot import to_pydot
from typing_inspect import is_union_type

import pynguin.configuration as config
import pynguin.utils.typetracing as tt
from pynguin.utils import randomness
from pynguin.utils.exceptions import ConfigurationException
from pynguin.utils.orderedset import OrderedSet
from pynguin.utils.type_utils import COLLECTIONS, PRIMITIVES

if typing.TYPE_CHECKING:
    from pynguin.analyses.module import TypeGuessingStats

_LOGGER = logging.getLogger(__name__)


# The following classes are inspired by
# https://github.com/python/mypy/blob/master/mypy/types.py and most likely incomplete.
# The plan is to gradually expand this type representation.


T = TypeVar("T")


class ProperType(ABC):
    """Base class for all types. Might have to add another layer, like mypy's Type?.

    All subclasses of this class are immutable."""

    @abstractmethod
    def accept(self, visitor: TypeVisitor) -> T:
        """Accept a type visitor

        Args:
            visitor: the visitor
        """

    def __str__(self) -> str:
        return self.accept(TypeStringVisitor())

    def __repr__(self) -> str:
        return self.accept(TypeReprVisitor())

    def __lt__(self, other):
        return str(self) < str(other)


class AnyType(ProperType):
    """The Any Type"""

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_any_type(self)

    def __hash__(self):
        return hash(AnyType)

    def __eq__(self, other):
        return isinstance(other, AnyType)


class NoneType(ProperType):
    """The None type"""

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_none_type(self)

    def __hash__(self):
        return hash(NoneType)

    def __eq__(self, other):
        return isinstance(other, NoneType)


class Instance(ProperType):
    """An instance type of form C[T1, ..., Tn].
    C is a class.
    Args can be empty."""

    def __init__(self, typ: TypeInfo, args: tuple[ProperType, ...] = None):
        assert typ.raw_type is not tuple, "Use TupleType instead!"
        self.type = typ
        if args is None:
            args = ()
        self.args: Final[tuple[ProperType, ...]] = tuple(args)
        # Cached hash value
        self._hash: int | None = None

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_instance(self)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash((self.type, self.args))
        return self._hash

    def __eq__(self, other):
        return (
            isinstance(other, Instance)
            and self.type == other.type
            and self.args == other.args
        )


class TupleType(ProperType):
    """Tuple type Tuple[T1, ..., Tn].
    Note that tuple is a special case and intentionally not
    `Instance(TypeInfo(tuple))` because tuple is varargs generic."""

    def __init__(self, args: tuple[ProperType, ...], unknown_size: bool = False):
        self.args: Final[tuple[ProperType, ...]] = args
        self.unknown_size: Final[bool] = unknown_size
        # Cached hash value
        self._hash: int | None = None

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_tuple_type(self)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash((self.args, self.unknown_size))
        return self._hash

    def __eq__(self, other):
        return (
            isinstance(other, TupleType)
            and self.args == other.args
            and self.unknown_size == other.unknown_size
        )


class UnionType(ProperType):
    """The union type Union[T1, ..., Tn] (at least one type argument)."""

    def __init__(self, items: tuple[ProperType, ...]):
        self.items: Final[tuple[ProperType, ...]] = items
        # TODO(fk) think about flattening Unions, also order should not matter.
        assert len(self.items) > 0
        # Cached hash value
        self._hash: int | None = None

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_union_type(self)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(self.items)
        return self._hash

    def __eq__(self, other):
        return isinstance(other, UnionType) and self.items == other.items


class Unsupported(ProperType):
    """Artificial type which represents a type that is currently not supported by
    our type abstraction. This is purely used for statistic purposes and should not
    be encountered during regular use."""

    def __eq__(self, other):
        return isinstance(other, Unsupported)

    def __hash__(self):
        return hash(Unsupported)

    def accept(self, visitor: TypeVisitor[T]) -> T:
        return visitor.visit_unsupported_type(self)


# Static to instances to avoid repeated construction.
ANY = AnyType()
NONE_TYPE = NoneType()
UNSUPPORTED = Unsupported()


class TypeVisitor(Generic[T]):
    """A type visitor.
    Note that the parameter of the visit_* methods is called 'left',
    because it makes the implementations of _SubTypeVisitor and _MaybeSubTypeVisitor
    more clear and Python does not like changing the names of parameters in subclasses,
    thus we renamed them in this class."""

    @abstractmethod
    def visit_any_type(self, left: AnyType) -> T:
        """Visit the Any type

        Args:
            left: the Any type

        Returns:
            result of the visit
        """

    @abstractmethod
    def visit_none_type(self, left: NoneType) -> T:
        """Visit the None type

        Args:
            left: the None type

        Returns:
            result of the visit
        """

    @abstractmethod
    def visit_instance(self, left: Instance) -> T:
        """Visit an instance

        Args:
            left: instance

        Returns:
            result of the visit
        """

    @abstractmethod
    def visit_tuple_type(self, left: TupleType) -> T:
        """Visit a tuple type

        Args:
            left: tuple

        Returns:
            result of the visit
        """

    @abstractmethod
    def visit_union_type(self, left: UnionType) -> T:
        """Visit a union

        Args:
            left: union

        Returns:
            result of the visit
        """

    @abstractmethod
    def visit_unsupported_type(self, left: Unsupported) -> T:
        """Visit unsupported type

        Args:
            left: unsupported

        Returns:
            result of the visit
        """


class TypeStringVisitor(TypeVisitor[str]):
    """A simple visitor to convert a proper type to a string."""

    def visit_any_type(self, left: AnyType) -> str:
        return "Any"

    def visit_none_type(self, left: NoneType) -> str:
        return "None"

    def visit_instance(self, left: Instance) -> str:
        rep = left.type.name if left.type.module == "builtins" else left.type.full_name
        if len(left.args) > 0:
            rep += "[" + self._sequence_str(left.args) + "]"
        return rep

    def visit_tuple_type(self, left: TupleType) -> str:
        rep = "tuple"
        if len(left.args) > 0:
            rep += "[" + self._sequence_str(left.args) + "]"
        return rep

    def visit_union_type(self, left: UnionType) -> str:
        if len(left.items) == 1:
            return left.items[0].accept(self)
        return self._sequence_str(left.items, sep=" | ")

    def _sequence_str(self, typs: Sequence[ProperType], sep=", ") -> str:
        return sep.join(t.accept(self) for t in typs)

    def visit_unsupported_type(self, left: Unsupported) -> str:
        return "<?>"


class TypeReprVisitor(TypeVisitor[str]):
    """A simple visitor to create a repr from a proper type."""

    def visit_any_type(self, left: AnyType) -> str:
        return "AnyType()"

    def visit_none_type(self, left: NoneType) -> str:
        return "NoneType()"

    def visit_instance(self, left: Instance) -> str:
        rep = f"Instance({left.type!r}"
        if len(left.args) > 0:
            rep += "(" + self._sequence_str(left.args) + ")"
        return rep + ")"

    def visit_tuple_type(self, left: TupleType) -> str:
        return f"TupleType({self._sequence_str(left.args)})"

    def visit_union_type(self, left: UnionType) -> str:
        return f"UnionType({self._sequence_str(left.items)})"

    def _sequence_str(self, typs: Sequence[ProperType]) -> str:
        return ", ".join(t.accept(self) for t in typs)

    def visit_unsupported_type(self, left: Unsupported) -> str:
        return "Unsupported()"


class _SubtypeVisitor(TypeVisitor[bool]):
    """A visitor to check the subtyping relationship between two types, i.e.,
    is left a subtype of right?

    There is no need to check 'right' for AnyType, as this is done outside.
    """

    def __init__(
        self,
        graph: TypeSystem,
        right: ProperType,
        sub_type_check: Callable[[ProperType, ProperType], bool],
    ):
        """Create new visitor

        Args:
            graph: The inheritance graph.
            right: The right type.
            sub_type_check: The subtype check to use
        """
        self.graph = graph
        self.right = right
        self.sub_type_check = sub_type_check

    def visit_any_type(self, left: AnyType) -> bool:  # pylint:disable=unused-argument
        # Any wins always
        return True

    def visit_none_type(self, left: NoneType) -> bool:  # pylint:disable=unused-argument
        # None cannot be subtyped
        # TODO(fk) handle protocols, e.g., hashable.
        return isinstance(self.right, NoneType)

    def visit_instance(self, left: Instance) -> bool:
        if isinstance(self.right, Instance):
            if not self.graph.is_subclass(left.type, self.right.type):
                return False
            if (
                left.type.num_hardcoded_generic_parameters
                == self.right.type.num_hardcoded_generic_parameters
                is not None
            ):
                # TODO(fk) handle generics properly :(
                # We only check hard coded generics for now and treat them as invariant,
                # i.e., set[T1] <: set[T2] <=> T1 <: T2 and T2 <: T1
                return all(
                    self.sub_type_check(left_elem, right_elem)
                    and self.sub_type_check(right_elem, left_elem)
                    for left_elem, right_elem in zip(left.args, self.right.args)
                )
            return True
        return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        if isinstance(self.right, TupleType):
            if len(left.args) != len(self.right.args):
                # TODO(fk) Handle unknown size.
                return False
            return all(
                self.sub_type_check(left_elem, right_elem)
                for left_elem, right_elem in zip(left.args, self.right.args)
            )
        return False

    def visit_union_type(self, left: UnionType) -> bool:
        return all(
            self.sub_type_check(left_elem, self.right) for left_elem in left.items
        )

    def visit_unsupported_type(self, left: Unsupported) -> bool:
        raise NotImplementedError("This type shall not be used during runtime")


class _MaybeSubtypeVisitor(_SubtypeVisitor):
    """A weaker subtype check, which only checks if left may be a subtype of right.
    For example, tuple[str | int | bytes, str | int | bytes] is not a subtype of
    tuple[int, int], but the actual return value may be."""

    def visit_union_type(self, left: UnionType) -> bool:
        return any(
            self.sub_type_check(left_elem, self.right) for left_elem in left.items
        )

    def visit_unsupported_type(self, left: Unsupported) -> bool:
        raise NotImplementedError("This type shall not be used during runtime")


class _CollectionTypeVisitor(TypeVisitor[bool]):

    Collections = {dict, list, set}  # No tuple because it is a separate type.

    def visit_any_type(self, left: AnyType) -> bool:
        return False

    def visit_none_type(self, left: NoneType) -> bool:
        return False

    def visit_instance(self, left: Instance) -> bool:
        return left.type.raw_type in _CollectionTypeVisitor.Collections

    def visit_tuple_type(self, left: TupleType) -> bool:
        return True

    def visit_union_type(self, left: UnionType) -> bool:
        return False

    def visit_unsupported_type(self, left: Unsupported) -> bool:
        raise NotImplementedError("This type shall not be used during runtime")


is_collection_type = _CollectionTypeVisitor()


class _PrimitiveTypeVisitor(TypeVisitor[bool]):

    Primitives = {int, str, bool, float, complex, bytes, type}

    def visit_any_type(self, left: AnyType) -> bool:
        return False

    def visit_none_type(self, left: NoneType) -> bool:
        return False

    def visit_instance(self, left: Instance) -> bool:
        return left.type.raw_type in _PrimitiveTypeVisitor.Primitives

    def visit_tuple_type(self, left: TupleType) -> bool:
        return False

    def visit_union_type(self, left: UnionType) -> bool:
        return False

    def visit_unsupported_type(self, left: Unsupported) -> bool:
        raise NotImplementedError("This type shall not be used during runtime")


is_primitive_type = _PrimitiveTypeVisitor()


# pylint:disable=too-many-instance-attributes
class TypeInfo:
    """A small wrapper around type, i.e., classes.
    Corresponds 1:1 to a class."""

    def __init__(self, raw_type: type):
        """Create type info from the given type.

        Don't use this constructor directly (unless for testing purposes), instead ask
        the inheritance graph to give you a type info for the given raw type.

        Naming in python is somehow misleading, 'type' actually only represents classes,
        but not any more complex types.

        Args:
            raw_type: the raw (class) type
        """
        self.raw_type = raw_type
        self.name = raw_type.__name__
        self.qualname = raw_type.__qualname__
        self.module = raw_type.__module__
        self.full_name = TypeInfo.to_full_name(raw_type)
        self.hash = hash(self.full_name)
        self.is_abstract = inspect.isabstract(raw_type)
        # TODO(fk) store more information on attributes
        self.instance_attributes: OrderedSet[str] = OrderedSet()
        self.symbols: OrderedSet[str] = OrderedSet()

        # TODO(fk) properly implement generics!
        # For now we just store the number of generic parameters for set, dict and list.
        self.num_hardcoded_generic_parameters: int | None = (
            2 if raw_type is dict else 1 if raw_type in (set, list) else None
        )

    @staticmethod
    def to_full_name(typ: type) -> str:
        """Get the full name of the given type

        Args:
            typ: The type for which we want a full name.

        Returns:
            The fully qualified name
        """
        return f"{typ.__module__}.{typ.__qualname__}"

    def __eq__(self, other) -> bool:
        return isinstance(other, TypeInfo) and other.full_name == self.full_name

    def __hash__(self):
        return self.hash

    def __repr__(self):
        return f"TypeInfo({self.full_name})"


@dataclass(eq=False, repr=False)
class InferredSignature:
    """Encapsulates the types inferred for a method."""

    # Signature inferred from inspect, only useful to get non-type related information
    # on parameters
    signature: inspect.Signature
    # The return type
    original_return_type: ProperType
    # A dict mapping every parameter name to its type
    original_parameters: dict[str, ProperType]

    # Proxy knowledge learned from executions
    knowledge: dict[str, tt.ProxyKnowledge] = field(
        default_factory=lambda: defaultdict(lambda: tt.ProxyKnowledge("ROOT")),
        init=False,
    )

    # Reference to the used type system.
    type_system: TypeSystem

    # Return type might be updated, which is stored here.
    return_type: ProperType = field(init=False)

    # The currently guessed parameter types. Guessing will never result in Any, as
    # that is not a useful guess.
    current_guessed_parameters: dict[str, list[ProperType]] = field(
        init=False, default_factory=dict
    )

    # Parameter types of each parameter, including unsupported types,
    # i.e., types that we currently cannot understand/parse. Purely used for statistics
    # purposes!
    parameters_for_statistics: dict[str, str] = field(default_factory=dict)
    return_type_for_statistics: str = ""

    def __post_init__(self):
        self.return_type = self.original_return_type

    def __str__(self):
        return str(self.signature)

    def get_parameter_types(
        self, signature_memo: dict[InferredSignature, dict[str, ProperType]]
    ) -> dict[str, ProperType]:
        """Get a possible type signature for the parameters.
        This method may choose a random type signature, or return the original one or
        create one based on the observed knowledge.

        Args:
            signature_memo: A memo that stores already chosen signatures, so that
                we don't choose another signature in the same run. This is required for
                certain operations in the test factory to be consistent.

        Returns:
            A dict of chosen parameter types for each parameter.
        """
        if (sig := signature_memo.get(self)) is not None:
            # We already chose a signature
            return sig
        res: dict[str, ProperType] = {}
        test_conf = config.configuration.test_creation
        for param_name, orig_type in self.original_parameters.items():
            if param_name in self.knowledge:
                # If we have information from proxies, update guess.
                self._update_guess(
                    param_name,
                    self._guess_parameter_type(
                        self.knowledge[param_name],
                        self.signature.parameters[param_name].kind,
                    ),
                )

            # Choose from:
            # - Reusing developer annotated types
            # - Guessed types from proxies
            # - NoneType
            # - AnyType, i.e., disregard type
            # TODO(fk) add choices from other source here, e.g., DeepTyper.
            choices: list[ProperType] = [NONE_TYPE, ANY]
            weights: list[float] = [test_conf.none_weight, test_conf.any_weight]
            # Only add the original type to the choices, if it is not Any.
            if not isinstance(orig_type, AnyType):
                choices.append(orig_type)
                weights.append(test_conf.original_type_weight)

            if (guessed := self.current_guessed_parameters.get(param_name)) is not None:
                choices.append(UnionType(tuple(sorted(guessed))))
                weights.append(test_conf.type_tracing_weight)
            chosen = randomness.choices(choices, weights)[0]

            if (
                randomness.next_float()
                < config.configuration.test_creation.wrap_var_param_type_probability
            ):
                # Wrap var-positional or var-keyword parameters in list/dict,
                # with a certain probability, as there might also be other data
                # structures suitable for being passed by * and **, e.g.,
                # generators, tuples, etc.
                chosen = self.type_system.wrap_var_param_type(
                    chosen,
                    self.signature.parameters[param_name].kind,
                )
            res[param_name] = chosen
        signature_memo[self] = res
        return res

    def _update_guess(self, name: str, guessed: ProperType | None):
        if guessed is None:
            return

        if (old := self.current_guessed_parameters.get(name)) is None:
            self.current_guessed_parameters[name] = [guessed]
        else:
            if guessed in old:
                return
            if len(old) >= 5:
                # Drop first guess
                old.pop(0)
            # append current
            old.append(guessed)

    # pylint:disable=too-many-return-statements
    def _guess_parameter_type(
        self, knowledge: tt.ProxyKnowledge, kind
    ) -> ProperType | None:
        """Guess a type for a parameter.

        Args:
            knowledge: The name of the parameter.
            kind: the kind of parameter.

        Returns:
            A guessed type for the given parameter name, or None, if no educated guess
                can be made.
        """
        match kind:
            case inspect.Parameter.VAR_KEYWORD:
                # Case for **kwargs parameter
                # We know that it is always dict[str, ?].
                # We can guess the unknown type by looking at the knowledge of
                # __getitem__ of the proxy.
                if (
                    get_item_knowledge := knowledge.symbol_table.get("__getitem__")
                ) is not None:
                    return self._guess_parameter_type_from(get_item_knowledge)
            case inspect.Parameter.VAR_POSITIONAL:
                # Case for *args parameter
                # We know that it is always list[?]
                # Similar to above.
                if (
                    iter_knowledge := knowledge.symbol_table.get("__iter__")
                ) is not None:
                    return self._guess_parameter_type_from(iter_knowledge)
            case _:
                return self._guess_parameter_type_from(knowledge)
        return None

    # If one of these methods was called on a proxy, we can use the argument type
    # to make guesses.
    # __mul__ and __rmul__ are not reliable, as they don't necessarily have to indicate
    # the type, for example, [1,2] * 3 is well-defined between a list and an int.
    _ARGUMENT_SYMBOLS = OrderedSet(
        [
            "__eq__",
            "__ne__",
            "__lt__",
            "__le__",
            "__gt__",
            "__ge__",
            "__add__",
            "__radd__",
            "__sub__",
            "__rsub__",
            "__truediv__",
            "__rtruediv__",
            "__floordiv__",
            "__rfloordiv__",
        ]
    )

    # We can guess the element type by looking at the knowledge from these
    _LIST_ELEMENT_SYMBOLS = OrderedSet(("__iter__", "__getitem__"))
    _DICT_KEY_SYMBOLS = OrderedSet(("__iter__",))
    _DICT_VALUE_SYMBOLS = OrderedSet(("__getitem__",))
    _SET_ELEMENT_SYMBOLS = OrderedSet(("__iter__",))
    _TUPLE_ELEMENT_SYMBOLS = OrderedSet(("__iter__", "__getitem__"))

    # We can guess generic type(s) from the argument type(s) of these methods:
    _LIST_ELEMENT_FROM_ARGUMENT_TYPES = OrderedSet(("__contains__", "__delitem__"))
    _SET_ELEMENT_FROM_ARGUMENT_TYPES = OrderedSet(("__contains__", "__delitem__"))
    _DICT_KEY_FROM_ARGUMENT_TYPES = OrderedSet(
        (
            "__contains__",
            "__delitem__",
            "__getitem__",
            "__setitem__",
        )
    )
    _DICT_VALUE_FROM_ARGUMENT_TYPES = OrderedSet(("__setitem__",))
    _TUPLE_ELEMENT_FROM_ARGUMENT_TYPES = OrderedSet(("__contains__",))

    # pylint:disable=invalid-name
    # Similar to above, but these are not dunder methods but are called,
    # e.g., for 'append', we need to search for 'append.__call__(...)'
    _LIST_ELEMENT_FROM_ARGUMENT_TYPES_PATH: OrderedSet[tuple[str, ...]] = OrderedSet(
        [("append", "__call__"), ("remove", "__call__")]
    )
    _SET_ELEMENT_FROM_ARGUMENT_TYPES_PATH: OrderedSet[tuple[str, ...]] = OrderedSet(
        [("add", "__call__"), ("remove", "__call__"), ("discard", "__call__")]
    )
    # Nothing for tuple and dict.
    _EMPTY_SET: OrderedSet[tuple[str, ...]] = OrderedSet()

    def _from_type_check(self, knowledge: tt.ProxyKnowledge) -> ProperType | None:
        # Type checks is not empty here.
        return self._choose_type_or_negate(
            OrderedSet(
                [
                    self.type_system.to_type_info(
                        randomness.choice(knowledge.type_checks)
                    )
                ]
            )
        )

    def _from_symbol_table(self, knowledge: tt.ProxyKnowledge) -> ProperType | None:
        random_symbol = randomness.choice(list(knowledge.symbol_table))
        if (
            random_symbol in InferredSignature._ARGUMENT_SYMBOLS
            and knowledge.symbol_table[random_symbol].arg_types[0]
            and randomness.next_float() < 0.5
        ):
            random_arg_type = randomness.choice(
                knowledge.symbol_table[random_symbol].arg_types[0]
            )
            return self._choose_type_or_negate(
                OrderedSet([self.type_system.to_type_info(random_arg_type)])
            )
        return self._choose_type_or_negate(
            self.type_system.find_by_symbol(random_symbol)
        )

    # pylint:disable=too-many-return-statements
    def _guess_parameter_type_from(
        self, knowledge: tt.ProxyKnowledge, recursion_depth: int = 0
    ) -> ProperType | None:
        guess_from: list[Callable[[tt.ProxyKnowledge], ProperType | None]] = []
        if knowledge.type_checks:
            guess_from.append(self._from_type_check)
        if knowledge.symbol_table:
            guess_from.append(self._from_symbol_table)

        if not guess_from:
            return None

        guessed_type: ProperType | None = randomness.choice(guess_from)(knowledge)

        if (
            recursion_depth <= 1
            and guessed_type
            and guessed_type.accept(is_collection_type)
        ):
            guessed_type = self._guess_generic_type_parameters_for_builtins(
                guessed_type, knowledge, recursion_depth
            )
        return guessed_type

    def _guess_generic_type_parameters_for_builtins(
        self,
        guessed_type: ProperType,
        knowledge: tt.ProxyKnowledge,
        recursion_depth: int,
    ):
        # If it is a builtin collection, we may be able to make further guesses on
        # the generic types.
        if isinstance(guessed_type, Instance):
            args = guessed_type.args
            match guessed_type.type.full_name:
                case "builtins.list":
                    guessed_element_type = self._guess_generic_arguments(
                        knowledge,
                        recursion_depth,
                        InferredSignature._LIST_ELEMENT_SYMBOLS,
                        InferredSignature._LIST_ELEMENT_FROM_ARGUMENT_TYPES,
                        InferredSignature._LIST_ELEMENT_FROM_ARGUMENT_TYPES_PATH,
                        argument_idx=0,
                    )
                    args = (
                        guessed_element_type
                        if guessed_element_type
                        else guessed_type.args[0],
                    )
                case "builtins.set":
                    guessed_element_type = self._guess_generic_arguments(
                        knowledge,
                        recursion_depth,
                        InferredSignature._SET_ELEMENT_SYMBOLS,
                        InferredSignature._SET_ELEMENT_FROM_ARGUMENT_TYPES,
                        InferredSignature._SET_ELEMENT_FROM_ARGUMENT_TYPES_PATH,
                        argument_idx=0,
                    )
                    args = (
                        guessed_element_type
                        if guessed_element_type
                        else guessed_type.args[0],
                    )
                case "builtins.dict":
                    guessed_key_type = self._guess_generic_arguments(
                        knowledge,
                        recursion_depth,
                        InferredSignature._DICT_KEY_SYMBOLS,
                        InferredSignature._DICT_KEY_FROM_ARGUMENT_TYPES,
                        InferredSignature._EMPTY_SET,
                        argument_idx=0,
                    )
                    guessed_value_type = self._guess_generic_arguments(
                        knowledge,
                        recursion_depth,
                        InferredSignature._DICT_VALUE_SYMBOLS,
                        InferredSignature._DICT_VALUE_FROM_ARGUMENT_TYPES,
                        InferredSignature._EMPTY_SET,
                        argument_idx=1,
                    )
                    args = (
                        guessed_key_type if guessed_key_type else guessed_type.args[0],
                        guessed_value_type
                        if guessed_value_type
                        else guessed_type.args[1],
                    )
            guessed_type = Instance(guessed_type.type, args)
        elif isinstance(guessed_type, TupleType):
            # Guess random size of tuple.
            num_elements = randomness.next_int(
                1, config.configuration.test_creation.collection_size
            )
            elements = []
            for _ in range(num_elements):
                guessed_element_type = self._guess_generic_arguments(
                    knowledge,
                    recursion_depth,
                    InferredSignature._TUPLE_ELEMENT_SYMBOLS,
                    InferredSignature._TUPLE_ELEMENT_FROM_ARGUMENT_TYPES,
                    InferredSignature._EMPTY_SET,
                    argument_idx=0,
                )
                if guessed_element_type:
                    elements.append(guessed_element_type)
                else:
                    elements.append(ANY)
            guessed_type = TupleType(tuple(elements))
        return guessed_type

    def _choose_type_or_negate(
        self, positive_types: OrderedSet[TypeInfo]
    ) -> ProperType | None:
        if not positive_types:
            return None

        if randomness.next_float() < config.configuration.test_creation.negate_type:
            negated_choices = self.type_system.get_type_outside_of(positive_types)
            if len(negated_choices) > 0:
                return self.type_system.make_instance(
                    randomness.choice(negated_choices)
                )
        return self.type_system.make_instance(randomness.choice(positive_types))

    # pylint:disable-next=too-many-arguments
    def _guess_generic_arguments(
        self,
        knowledge: tt.ProxyKnowledge,
        recursion_depth: int,
        element_symbols: OrderedSet[str],
        argument_symbols: OrderedSet[str],
        argument_symbols_paths: OrderedSet[tuple[str, ...]],
        argument_idx: int,
    ) -> ProperType | None:
        guess_from: list[
            Callable[
                [],
                ProperType | None,
            ]
        ] = []

        if elem_symbols := element_symbols.intersection(knowledge.symbol_table.keys()):
            guess_from.append(
                functools.partial(
                    self._guess_parameter_type_from,
                    knowledge.symbol_table[randomness.choice(elem_symbols)],
                    recursion_depth + 1,
                )
            )
        if arg_symbols := argument_symbols.intersection(knowledge.symbol_table.keys()):
            guess_from.append(
                functools.partial(
                    self._guess_from_argument_types,
                    arg_symbols,
                    knowledge,
                    argument_idx,
                )
            )
        if paths := [
            path
            for path in argument_symbols_paths
            if knowledge.find_path(path) is not None
        ]:
            guess_from.append(
                functools.partial(
                    self._guess_from_argument_types_from_path, paths, knowledge
                )
            )
        # Add Any as guess, i.e., do not make argument more specific
        guess_from.append(lambda: ANY)

        return randomness.choice(guess_from)()

    def _guess_from_argument_types(
        self,
        arg_symbols: OrderedSet[str],
        knowledge: tt.ProxyKnowledge,
        arg_idx: int = 0,
    ) -> ProperType | None:
        arg_types = knowledge.symbol_table[randomness.choice(arg_symbols)].arg_types[
            arg_idx
        ]
        if arg_types:
            return self._choose_type_or_negate(
                OrderedSet(
                    [self.type_system.to_type_info(randomness.choice(arg_types))]
                )
            )
        return None

    def _guess_from_argument_types_from_path(
        self,
        paths: list[tuple[str, ...]],
        knowledge: tt.ProxyKnowledge,
    ) -> ProperType | None:
        path = randomness.choice(paths)
        path_end = knowledge.find_path(path)
        assert path_end is not None
        # Select type from last element in path, i.e., '__call__'
        # This way we can, for example, guess the generic type by looking at the
        # argument types of `append.__call__`.
        arg_types = path_end.symbol_table[path[-1]].arg_types[0]
        if arg_types:
            return self._choose_type_or_negate(
                OrderedSet(
                    [self.type_system.to_type_info(randomness.choice(arg_types))]
                )
            )
        return None

    def log_stats_and_guess_signature(
        self, is_constructor: bool, callable_full_name: str, stats: TypeGuessingStats
    ) -> None:
        """Logs some statistics and creates a guessed signature.
        Parameters annotated with Any could not be guessed.

        Parameters:
            callable_full_name: The full, unique name of the callable.
            is_constructor: does this signature to a constructor?
            stats: stats object to log to.
        """
        stats.annotated_parameter_types[callable_full_name] = dict(
            self.parameters_for_statistics
        )
        if not is_constructor:
            # Constructors don't need a return type, so no need to log it.
            stats.annotated_return_types[
                callable_full_name
            ] = self.return_type_for_statistics
        else:
            stats.number_of_constructors += 1

        parameter_types: dict[str, str] = {}
        parameters = []
        for param_name, param in self.signature.parameters.items():
            param_annotation: ProperType = ANY
            if param_name in self.original_parameters and param_name in self.knowledge:
                counter: Counter[ProperType] = Counter()
                for _ in range(100):
                    guess = self._guess_parameter_type(
                        self.knowledge[param_name],
                        param.kind,
                    )
                    if guess is not None:
                        counter[guess] += 1
                if len(counter) > 0:
                    top_guess = counter.most_common(1)[0][0]
                    param_annotation = top_guess
            # No guess or no knowledge
            parameters.append(param.replace(annotation=str(param_annotation)))
            if param_name in self.original_parameters:
                parameter_types[param_name] = str(param_annotation)
        return_type = str(self.return_type)
        if not is_constructor and self.return_type != self.original_return_type:
            # Only when we recorded something that is not a constructor:
            stats.recorded_return_types[callable_full_name] = return_type
        stats.guessed_parameter_types[callable_full_name] = parameter_types
        stats.formatted_guessed_signatures[
            callable_full_name
        ] = callable_full_name + str(
            self.signature.replace(parameters=parameters, return_annotation=return_type)
        )


class TypeSystem:  # pylint:disable=too-many-public-methods
    """Provides a simple inheritance graph relating various classes using their subclass
    relationships. Note that parents point to their children.

    This is also the central system to store/handle type information.
    """

    def __init__(self):
        self._graph = nx.DiGraph()
        # Maps all known types from their full name to their type info.
        self._types: dict[str, TypeInfo] = {}
        # Maps symbols to type which have that symbol
        self._symbol_map: dict[str, OrderedSet[TypeInfo]] = defaultdict(OrderedSet)
        # These types are intrinsic for Pynguin, i.e., we can generate them ourselves
        # without needing a generator. We store them here, so we don't have to generate
        # them all the time.
        self.primitive_proper_types = [
            self.convert_type_hint(prim) for prim in PRIMITIVES
        ]
        self.collection_proper_types = [
            self.convert_type_hint(coll) for coll in COLLECTIONS
        ]
        # Pre-compute numeric tower
        numeric = [complex, float, int, bool]
        self.numeric_tower: dict[Instance, list[Instance]] = cast(
            dict[Instance, list[Instance]],
            {
                self.convert_type_hint(typ): [
                    self.convert_type_hint(tp) for tp in numeric[idx:]
                ]
                for idx, typ in enumerate(numeric)
            },
        )

    def enable_numeric_tower(self):
        """Enable the numeric tower on this type system."""
        # Enable numeric tower int <: float <: complex.
        # https://peps.python.org/pep-0484/#the-numeric-tower
        bool_info = self.to_type_info(bool)
        int_info = self.to_type_info(int)
        float_info = self.to_type_info(float)
        complex_info = self.to_type_info(complex)
        self.add_subclass_edge(super_class=int_info, sub_class=bool_info)
        self.add_subclass_edge(super_class=float_info, sub_class=int_info)
        self.add_subclass_edge(super_class=complex_info, sub_class=float_info)

    def add_subclass_edge(self, *, super_class: TypeInfo, sub_class: TypeInfo) -> None:
        """Add a subclass edge between two types.

        Args:
            super_class: superclass
            sub_class: subclass
        """
        self._graph.add_edge(super_class, sub_class)

    @functools.lru_cache(maxsize=1024)
    def get_subclasses(self, klass: TypeInfo) -> OrderedSet[TypeInfo]:
        """Provides all descendants of the given type. Includes klass.

        Args:
            klass: The class whose subtypes we want to query.

        Returns:
            All subclasses including klass
        """
        if klass not in self._graph:
            return OrderedSet([klass])
        result: OrderedSet[TypeInfo] = OrderedSet(nx.descendants(self._graph, klass))
        result.add(klass)
        return result

    @functools.lru_cache(maxsize=1024)
    def get_superclasses(self, klass: TypeInfo) -> OrderedSet[TypeInfo]:
        """Provides all ancestors of the given class.

        Args:
            klass: The class whose supertypes we want to query.

        Returns:
            All superclasses including klass
        """
        if klass not in self._graph:
            return OrderedSet([klass])
        result: OrderedSet[TypeInfo] = OrderedSet(nx.ancestors(self._graph, klass))
        result.add(klass)
        return result

    def get_type_outside_of(
        self, klasses: OrderedSet[TypeInfo]
    ) -> OrderedSet[TypeInfo]:
        """Find a type that does not belong to the given types or any subclasses.

        Args:
            klasses: The classes to exclude

        Returns:
            A set of klasses that don't belong the given ones.
        """
        results = OrderedSet(self._types.values())
        for info in klasses:
            results.difference_update(self.get_subclasses(info))
        return results

    @functools.lru_cache(maxsize=16384)
    def is_subclass(self, left: TypeInfo, right: TypeInfo) -> bool:
        """Is 'left' a subclass of 'right'?

        Args:
            left: left type info
            right: right type info

        Returns:
            True, if there is a subclassing path from left to right.
        """
        return nx.has_path(self._graph, right, left)

    @functools.lru_cache(maxsize=16384)
    def is_subtype(self, left: ProperType, right: ProperType) -> bool:
        """Is 'left' a subtype of 'right'?

        This check is more than incomplete, but it takes into account
        that anything is a subtype of AnyType.

        See https://peps.python.org/pep-0483/ and https://peps.python.org/pep-0484/
        for more details

        Args:
            left: The left type
            right: The right type

        Returns:
            True, if left is a subtype of right.
        """
        if isinstance(right, AnyType):
            # trivial case
            return True
        if isinstance(right, UnionType) and not isinstance(left, UnionType):
            # Case that would be duplicated for each type, so we put it here.
            return any(self.is_subtype(left, right_elem) for right_elem in right.items)
        return left.accept(_SubtypeVisitor(self, right, self.is_subtype))

    @functools.lru_cache(maxsize=16384)
    def is_maybe_subtype(self, left: ProperType, right: ProperType) -> bool:
        """Is 'left' maybe a subtype of 'right'?

        This is a more lenient check than is_subtype. Consider a function that
        returns tuple[str | int | bytes, str | int | bytes]. Strictly speaking, we
        cannot use such a value as an argument for a function that requires an argument
        of type tuple[int, int]. However, it may be possible that the returned
        value is tuple[int, int], in which case it does work.
        This check only differs from is_subtype in how it handles Unions.
        Instead of requiring every type to be a subtype, it is sufficient that one
        type of the Union is a subtype.

        Args:
            left: The left type
            right: The right type

        Returns:
            True, if left may be a subtype of right.
        """
        if isinstance(right, AnyType):
            # trivial case
            return True
        if isinstance(right, UnionType) and not isinstance(left, UnionType):
            # Case that would be duplicated for each type, so we put it here.
            return any(
                self.is_maybe_subtype(left, right_elem) for right_elem in right.items
            )
        return left.accept(_MaybeSubtypeVisitor(self, right, self.is_maybe_subtype))

    @property
    def dot(self) -> str:
        """Create dot representation of this graph.

        Returns:
            A dot string.
        """
        dot = to_pydot(self._graph)
        return dot.to_string()

    def to_type_info(self, typ: type) -> TypeInfo:
        """Find or create type info for the given type.

        Args:
            typ: The raw type we want to convert.

        Returns:
            A type info object.
        """
        # TODO(fk) what to do when we encounter a new type?
        found = self._types.get(TypeInfo.to_full_name(typ))
        if found is not None:
            return found
        info = TypeInfo(typ)
        self._types[info.full_name] = info
        self._graph.add_node(info)
        return info

    def find_type_info(self, full_name: str) -> TypeInfo | None:
        """Find typeinfo for the given name.

        Args:
            full_name: The name to search for.

        Returns:
            Type info, if any.
        """
        return self._types.get(full_name)

    def find_by_symbol(self, symbol: str) -> OrderedSet[TypeInfo]:
        """Search for all types that have the given symbol.

        Args:
            symbol: the symbol to search for.

        Returns:
            All types (or supertypes thereof) who have the given symbol.
        """
        return self._symbol_map[symbol]

    @functools.lru_cache(maxsize=1)
    def get_all_types(self) -> list[TypeInfo]:
        """Provides a list of all known types.

        Returns:
            A list of all known types.
        """
        return list(self._types.values())

    def push_symbols_down(self) -> None:
        """We don't want to see symbols multiple times, e.g., in subclasses, so only the
        first class in the hierarchy which adds the symbol should have it listed as a
        symbol, i.e., when searching for a class with that symbol we only want to
        retrieve the top-most class(es) in the hierarchy which define it, and not every
        (sub)class that inherited it.
        """
        reach_in_sets: dict[TypeInfo, set[str]] = defaultdict(set)
        reach_out_sets: dict[TypeInfo, set[str]] = defaultdict(set)

        # While object sits at the top, it is not particularly useful, so we delete
        # some of it symbols, as they are only stubs. For example, when searching for
        # an object that supports comparison, choosing object does not make sense,
        # because it will raise a NotImplementedError.
        object_info = self.find_type_info("builtins.object")
        assert object_info is not None
        object_info.symbols.difference_update({"__lt__", "__le__", "__gt__", "__ge__"})

        # Use fix point iteration with reach-in/out to push elements down.
        work_list = list(self._graph.nodes)
        while len(work_list) > 0:
            current = work_list.pop()
            old_val = set(reach_out_sets[current])
            for pred in self._graph.predecessors(current):
                reach_in_sets[current].update(reach_out_sets[pred])
            current.symbols.difference_update(reach_in_sets[current])
            reach_out_sets[current] = set(reach_in_sets[current])
            reach_out_sets[current].update(current.symbols)
            if old_val != reach_out_sets[current]:
                work_list.extend(self._graph.successors(current))
        for type_info in self._graph.nodes:
            for symbol in type_info.symbols:
                self._symbol_map[symbol].add(type_info)

    def wrap_var_param_type(self, typ: ProperType, param_kind) -> ProperType:
        """Wrap the parameter type of *args and **kwargs in List[...] or Dict[str, ...],
        respectively.

        Args:
            typ: The type to be wrapped.
            param_kind: the kind of parameter.

        Returns:
            The wrapped type, or the original type, if no wrapping is required.
        """
        if param_kind == inspect.Parameter.VAR_POSITIONAL:
            return Instance(self.to_type_info(list), (typ,))
        if param_kind == inspect.Parameter.VAR_KEYWORD:
            return Instance(self.to_type_info(dict), (self.convert_type_hint(str), typ))
        return typ

    def infer_type_info(
        self,
        method: Callable,
        type_inference_strategy=config.TypeInferenceStrategy.TYPE_HINTS,
    ) -> InferredSignature:
        """Infers the type information for a callable.

        Args:
            method: The callable we try to infer type information for
            type_inference_strategy: Whether to incorporate type annotations

        Returns:
            The inference result

        Raises:
            ConfigurationException: in case an unknown type-inference strategy was
                selected
        """
        match type_inference_strategy:
            case config.TypeInferenceStrategy.TYPE_HINTS:
                return self.infer_signature(method, self.type_hints_provider)
            case config.TypeInferenceStrategy.NONE:
                return self.infer_signature(method, self.no_type_hints_provider)
            case _:
                raise ConfigurationException(
                    f"Unknown type-inference strategy {type_inference_strategy}"
                )

    @staticmethod
    def no_type_hints_provider(_: Callable) -> dict[str, Any]:
        """Provides no type hints.

        Args:
            _: Ignored.

        Returns:
            An empty dict.
        """
        return {}

    @staticmethod
    def type_hints_provider(method: Callable) -> dict[str, Any]:
        """Provides PEP484-style type information, if available.

        Args:
            method: The method for which we want type hints.

        Returns:
            A dict mapping parameter names to type hints.
        """
        try:
            hints = get_type_hints(method)
            # Sadly there is no guarantee that resolving the type hints actually works.
            # If the developers annotated something with an erroneous type hint we fall
            # back to no type hints, i.e., use Any.
            # The import used in the type hint could also be conditional on
            # typing.TYPE_CHECKING, e.g., to avoid circular imports, in which case this
            # also fails.
        except NameError:
            hints = {}
        return hints

    def infer_signature(
        self, method: Callable, type_hint_provider: Callable[[Callable], dict]
    ) -> InferredSignature:
        """Infers the method signature using the given type hint provider.

        Args:
            method: The callable
            type_hint_provider: A method that provides type hints for the given method.

        Returns:
            The inference result
        """
        if inspect.isclass(method) and hasattr(method, "__init__"):
            return self.infer_signature(getattr(method, "__init__"), type_hint_provider)

        method_signature = inspect.signature(method)
        hints = type_hint_provider(method)
        parameters: dict[str, ProperType] = {}
        parameters_for_statistics: dict[str, str] = {}
        for param_name in method_signature.parameters:
            if param_name == "self":
                # TODO(fk) does not necessarily work, can be named anything,
                #  for example cls for @classmethod.
                continue
            parameters[param_name] = self.convert_type_hint(hints.get(param_name))
            parameters_for_statistics[param_name] = str(
                self.convert_type_hint(hints.get(param_name), unsupported=UNSUPPORTED)
            )

        return_type: ProperType = self.convert_type_hint(hints.get("return"))
        return_type_for_statistics: ProperType = self.convert_type_hint(
            hints.get("return"), unsupported=UNSUPPORTED
        )

        return InferredSignature(
            signature=method_signature,
            original_parameters=parameters,
            original_return_type=return_type,
            type_system=self,
            parameters_for_statistics=parameters_for_statistics,
            return_type_for_statistics=str(return_type_for_statistics),
        )

    def convert_type_hint(self, hint: Any, unsupported: ProperType = ANY) -> ProperType:
        # pylint:disable=too-many-return-statements
        """Python's builtin functionality makes handling types during runtime really
        hard, because 1) this is not intended to be used at runtime and 2) there are a
        lot of different notations, due to the constantly evolving type hint system.
        We also cannot easily use mypy's type abstraction because it is 1) strongly
        encapsulated and not part of mypy's public API and 2) is designed to be used
        for static type checking. This method tries to translate type hints into our
        own type abstraction in order to make handling types less painful.

        This conversion is naive when compared to what sophisticated type checkers like
        mypy do, but it is hopefully sufficient for our purposes.
        This method only handles a very small subset of the types that we may
        encounter in the wild, but at least it allows use to better reason about types.
        This should be extended in the future to handle more cases.

        Args:
            hint: The type hint
            unsupported: The type to use when encountering an unsupported type construct

        Returns:
            A proper type.
        """
        # We must handle a lot of special cases, so try to give an example for each one.
        if hint is Any or hint is None:
            # typing.Any or empty
            return ANY
        if hint is type(None):  # noqa: E721
            # None
            return NONE_TYPE
        if hint is tuple:
            # tuple
            # TODO(fk) Tuple without size. Should use tuple[Any, ...] ?
            #  But ... (ellipsis) is not a type.
            return TupleType((ANY,), unknown_size=True)
        if get_origin(hint) is tuple:
            # tuple[int, str] or typing.Tuple[int, str] or typing.Tuple
            args = self.__convert_args_if_exists(hint, unsupported=unsupported)
            if not args:
                return TupleType((ANY,), unknown_size=True)
            return TupleType(args)
        if is_union_type(hint) or isinstance(hint, types.UnionType):
            # int | str or typing.Union[int, str]
            # TODO(fk) don't make a union including Any.
            return UnionType(
                tuple(
                    sorted(self.__convert_args_if_exists(hint, unsupported=unsupported))
                )
            )
        if isinstance(hint, (_BaseGenericAlias, types.GenericAlias)):
            # list[int, str] or List[int, str] or Dict[int, str] or set[str]
            result = Instance(
                self.to_type_info(hint.__origin__),
                self.__convert_args_if_exists(hint, unsupported=unsupported),
            )
            # TODO(fk) remove this one day.
            #  Hardcoded support generic dict, list and set.
            return self._fixup_known_generics(result)

        if isinstance(hint, type):
            # int or str or MyClass
            return self._fixup_known_generics(Instance(self.to_type_info(hint)))
        # TODO(fk) log unknown hints to so we can better understand what
        #  we should add next
        #  Remove this or log to statistics?
        _LOGGER.debug("Unknown type hint: %s", hint)
        # Should raise an error in the future.
        return unsupported

    def __convert_args_if_exists(
        self, hint: Any, unsupported: ProperType
    ) -> tuple[ProperType, ...]:
        if hasattr(hint, "__args__"):
            return tuple(
                self.convert_type_hint(t, unsupported=unsupported)
                for t in hint.__args__
            )
        return ()

    def make_instance(self, typ: TypeInfo) -> Instance | TupleType | NoneType:
        """Create an instance from the given type.

        Args:
            typ: The type info.

        Returns:
            An instance or TupleType
        """
        if typ.full_name == "builtins.tuple":
            return TupleType((ANY,), unknown_size=True)
        if typ.full_name == "builtins.NoneType":
            return NONE_TYPE
        result = Instance(
            typ,
        )
        return self._fixup_known_generics(result)

    @staticmethod
    def _fixup_known_generics(result: Instance) -> Instance:
        if result.type.num_hardcoded_generic_parameters is not None:
            args = tuple(result.args)
            if len(result.args) < result.type.num_hardcoded_generic_parameters:
                # Fill with AnyType if to small
                args = args + (ANY,) * (
                    result.type.num_hardcoded_generic_parameters - len(args)
                )
            elif len(result.args) > result.type.num_hardcoded_generic_parameters:
                # Remove excessive args.
                args = args[: result.type.num_hardcoded_generic_parameters]
            return Instance(result.type, args)
        return result
