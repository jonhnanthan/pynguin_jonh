#  This file is part of Pynguin.
#
#  SPDX-FileCopyrightText: 2019–2022 Pynguin Contributors
#
#  SPDX-License-Identifier: LGPL-3.0-or-later
#
from tests.fixtures.cluster.async_class_gen import Foo
from tests.fixtures.cluster.async_class_method import Foo as Foo2
from tests.fixtures.cluster.async_func import foo as foo2
from tests.fixtures.cluster.async_gen import foo


def func():
    print(foo)
    print(foo2)
    print(Foo)
    print(Foo2)
