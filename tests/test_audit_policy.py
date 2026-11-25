from __future__ import annotations

import ctypes

import pytest

from backend.sandbox.native_windows import audit_policy


class PolicyApi:
    def __init__(self, *, error: int = 0, flags: tuple[int, int] | None = None) -> None:
        self.error = error
        self.existing = (audit_policy._Policy * 2)() if flags is not None else None
        if flags is not None:
            for index, value in enumerate(flags):
                self.existing[index].flags = value
        self.written = None
        self.freed = False

    def AuditQueryPerUserPolicy(self, _sid, _guids, _count, output):
        if self.error:
            return False
        pointer = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(audit_policy._Policy)))
        pointer[0] = ctypes.cast(self.existing, ctypes.POINTER(audit_policy._Policy))
        return True

    def AuditSetPerUserPolicy(self, _sid, policies, count):
        self.written = tuple(policies[index].flags for index in range(count))
        return True

    def AuditFree(self, _pointer):
        self.freed = True


def policy_with_api(monkeypatch, api):
    monkeypatch.setattr(audit_policy.ctypes, "get_last_error", lambda: api.error, raising=False)
    policy = object.__new__(audit_policy.WindowsAuditPolicy)
    policy.api = api
    return policy


def test_missing_per_user_policy_is_initialized(monkeypatch):
    api = PolicyApi(error=2)
    policy_with_api(monkeypatch, api)._ensure_user_policy(None)
    assert api.written == (4, 4)
    assert not api.freed


@pytest.mark.parametrize("error", [5, 87, 1314])
def test_per_user_policy_query_errors_are_not_treated_as_missing(monkeypatch, error):
    api = PolicyApi(error=error)
    with pytest.raises(audit_policy.SandboxAuditUnavailable, match=f"code {error}"):
        policy_with_api(monkeypatch, api)._ensure_user_policy(None)
    assert api.written is None


def test_existing_per_user_policy_preserves_success_and_enables_failures(monkeypatch):
    api = PolicyApi(flags=(0x19, 0))
    policy_with_api(monkeypatch, api)._ensure_user_policy(None)
    assert api.written == (5, 4)
    assert api.freed


class SaclApi:
    def __init__(self, error):
        self.error = error
        self.audit_ace = None
        self.written_type = None

    def AuditQueryGlobalSaclW(self, _object_type, _output):
        return False

    def InitializeAcl(self, _buffer, _size, _revision):
        return True

    def AddAuditAccessAceEx(self, _buffer, _revision, _flags, mask, _sid, success, failure):
        self.audit_ace = (mask, success, failure)
        return True

    def AuditSetGlobalSaclW(self, object_type, _buffer):
        self.written_type = object_type
        return True


@pytest.mark.parametrize("object_type,mask", [("File", 0x001F01FF), ("Key", 0x000F003F)])
def test_missing_global_sacl_is_initialized_with_failure_auditing(monkeypatch, object_type, mask):
    api = SaclApi(2)
    policy = policy_with_api(monkeypatch, api)
    policy._ensure_global_sacl(object_type, ctypes.create_string_buffer(b"account-sid"), mask)
    assert api.audit_ace == (mask, False, True)
    assert api.written_type == object_type


def test_global_sacl_access_denied_does_not_replace_policy(monkeypatch):
    api = SaclApi(5)
    policy = policy_with_api(monkeypatch, api)
    with pytest.raises(audit_policy.SandboxAuditUnavailable, match="code 5"):
        policy._ensure_global_sacl("File", ctypes.create_string_buffer(b"account-sid"), 0x001F01FF)
    assert api.audit_ace is None
    assert api.written_type is None
