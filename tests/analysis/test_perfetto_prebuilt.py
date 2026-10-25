import importlib
import sys

import pytest

from gcmon.analysis.perfetto_prebuilt import MissingExtraError, trace_processor_bin


def test_the_module_imports_without_the_extra(without_perfetto: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "gcmon.analysis.perfetto_prebuilt")

    importlib.import_module("gcmon.analysis.perfetto_prebuilt")


def test_without_the_extra_the_shell_is_refused_naming_it(without_perfetto: None) -> None:
    with pytest.raises(MissingExtraError, match=r"gcmon\[analysis\]"):
        trace_processor_bin()
