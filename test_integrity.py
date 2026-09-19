
"""Core integrity tests — python tests/test_integrity.py"""
import sys
import types
from pathlib import Path

class _Col:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def __getattr__(self, n):
        def _noop(*a, **k): return None
        return _noop

class _FakeSt:
    session_state = {}
    def columns(self, *a, **k):
        n = a[0] if a else 2
        if isinstance(n, (list, tuple)):
            return tuple(_Col() for _ in n)
        return tuple(_Col() for _ in range(int(n) if n else 2))
    def __getattr__(self, name):
        def _noop(*a, **k):
            return _Col() if name in ("form", "expander", "sidebar", "container") else None
        return _noop

sys.modules["streamlit"] = _FakeSt()

class _FakeBcrypt:
    def gensalt(self, *a, **k): return b"$2b$12$fakesaltfakesaltfake"
    def hashpw(self, password, salt): return b"$2b$12$fakehash"
    def checkpw(self, password, hashed): return True
sys.modules["bcrypt"] = _FakeBcrypt()

# Prevent app main from running
import builtins
_orig = builtins.__import__
def _import(name, *a, **k):
    return _orig(name, *a, **k)
# Load app source with main guarded
root = Path(__file__).resolve().parents[1]
src = (root / "app.py").read_text()
# Stop before MAIN APP section
cut = src.find("# MAIN APP")
if cut < 0:
    cut = src.find('if "user" not in st.session_state')
code = src[:cut] if cut > 0 else src
ns = {"__name__": "app_test", "__file__": str(root / "app.py")}
sys.path.insert(0, str(root))
exec(compile(code, "app.py", "exec"), ns)

def test_reporting_rate_audit():
    reporting_rate_audit = ns["reporting_rate_audit"]
    assert reporting_rate_audit(0.5)["status"] == "valid"
    assert reporting_rate_audit(0.5)["value"] == 50.0
    assert reporting_rate_audit(2.0)["status"] == "invalid"
    assert reporting_rate_audit(1.2)["status"] == "invalid"
    assert reporting_rate_audit(85)["status"] == "valid"
    assert reporting_rate_audit(100)["status"] == "valid"
    assert reporting_rate_audit(101)["status"] == "invalid"
    assert reporting_rate_audit(-1)["status"] == "invalid"

def test_user_scope_filter():
    import pandas as pd
    apply_user_data_scope = ns["apply_user_data_scope"]
    df = pd.DataFrame({
        "facility": ["Jaribuni", "Pingilikani", "Mwakuhenga"],
        "sub_county": ["Ganze", "Kilifi South", "Kilifi North"],
        "value": [1, 2, 3],
        "data_status": ["Actual", "Actual", "Actual"],
    })
    out = apply_user_data_scope(df, {"facility_scope": "Jaribuni", "scope": "facility"})
    assert list(out["facility"].unique()) == ["Jaribuni"]
    out2 = apply_user_data_scope(df, {"sub_county_scope": "Ganze", "scope": "sub_county"})
    assert list(out2["facility"].unique()) == ["Jaribuni"]

def test_not_yet_due_excluded():
    import pandas as pd
    apply_user_data_scope = ns["apply_user_data_scope"]
    df = pd.DataFrame({
        "facility": ["Jaribuni", "Jaribuni"],
        "value": [1, 2],
        "data_status": ["Actual", "NOT_YET_DUE"],
    })
    out = apply_user_data_scope(df, {"scope": "all"})
    assert len(out) == 1

if __name__ == "__main__":
    test_reporting_rate_audit()
    print("reporting_rate_audit OK")
    test_user_scope_filter()
    print("RBAC scope OK")
    test_not_yet_due_excluded()
    print("NOT_YET_DUE exclusion OK")
    print("ALL PASS")
