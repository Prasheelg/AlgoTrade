"""broker: TOTP codes, unattended login, error handling and the --kotak-check command. The SDK is faked."""
import base64
import json
import sys

import neo_api_client
import pytest

import main
from algotrade import broker, config

SECRET = base64.b32encode(b"12345678901234567890").decode()   # RFC 6238 test key
CREDS = dict(KOTAK_CONSUMER_KEY="ck", KOTAK_MOBILE="+919999999999", KOTAK_UCC="AB123", KOTAK_MPIN="4321",
             KOTAK_TOTP_SECRET=SECRET)


@pytest.mark.parametrize("at,code", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")])
def test_totp_matches_rfc6238_vectors(at, code):
    assert broker.totp(SECRET, at=at) == code


def test_totp_accepts_spaces_lowercase_and_missing_padding():
    s = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
    assert broker.totp(s, at=59) == "287082"
    assert broker.totp("JBSWY3DPEHPK3PXP", at=0) == broker.totp("jbswy3dpehpk3pxp", at=0)


class FakeNeo:
    instances = []

    def __init__(self, consumer_key=None, environment=None):
        self.calls = [("init", consumer_key, environment)]
        self.fail = FakeNeo.fail_next.pop(0) if FakeNeo.fail_next else None
        FakeNeo.instances.append(self)

    def totp_login(self, mobile_number=None, ucc=None, totp=None):
        self.calls.append(("totp_login", mobile_number, ucc, totp))
        if self.fail == "login":
            return {"Error": "Invalid TOTP"}
        return {"data": {"token": "t", "sid": "s", "greetingName": "TEST USER", "kType": "View"}}

    def totp_validate(self, mpin=None):
        self.calls.append(("totp_validate", mpin))
        return {"data": {"kType": "Trade"}}

    def limits(self):
        self.calls.append(("limits",))
        if self.fail == "limits":
            self.fail = None
            return {"Error Message": "Complete the 2fa process before accessing this application"}
        return {"Net": "10000.00", "stat": "Ok"}

    def holdings(self):
        return {"data": [{"symbol": "TCS"}]}

    def positions(self):
        return {"data": []}

    def order_report(self, order_id=None):
        return {"data": [{"nOrdNo": "1"}, {"nOrdNo": "2"}]}


@pytest.fixture
def neo(monkeypatch):
    for k, v in CREDS.items():
        monkeypatch.setattr(config, k, v)
    FakeNeo.instances, FakeNeo.fail_next = [], []
    monkeypatch.setattr(neo_api_client, "NeoAPI", FakeNeo)
    monkeypatch.setattr(broker.time, "time", lambda: 59)
    return FakeNeo


def test_login_sequence_and_daily_reuse(neo):
    c = broker.login()
    assert c.calls == [("init", "ck", "prod"), ("totp_login", "+919999999999", "AB123", "287082"),
                       ("totp_validate", "4321")]
    assert broker.login() is c and len(neo.instances) == 1
    assert broker._session["name"] == "TEST USER"


def test_missing_credentials(monkeypatch):
    assert broker.missing_credentials() == ["KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN",
                                           "KOTAK_TOTP_SECRET"]
    with pytest.raises(broker.BrokerError, match="Missing in .env"):
        broker.login()


def test_failed_login_raises_without_secrets(neo):
    neo.fail_next = ["login"]
    with pytest.raises(broker.BrokerError) as e:
        broker.login()
    assert "Invalid TOTP" in str(e.value)
    for secret in ("4321", SECRET, "287082", "+919999999999"):
        assert secret not in str(e.value)
    assert broker._session["client"] is None


def test_call_logs_in_again_once_on_failure(neo):
    neo.fail_next = ["limits"]
    assert broker.call("limits") == {"Net": "10000.00", "stat": "Ok"}
    assert len(neo.instances) == 2          # first session failed, second login succeeded


def test_call_gives_up_after_second_failure(neo, monkeypatch):
    monkeypatch.setattr(FakeNeo, "limits", lambda self: {"Error": "down"})
    with pytest.raises(broker.BrokerError, match="limits failed: down"):
        broker.call("limits")
    assert len(neo.instances) == 2


def test_no_data_responses_are_empty_lists_not_errors():
    # both forms, as returned by the real API for an empty account
    assert broker._check({"error": [{"code": 424, "message": "No holdings found for this client AB123"}]},
                         "holdings") == {"data": []}
    assert broker._check({"stCode": 5203, "errMsg": "No Data", "desc": "data not found", "stat": "Not_Ok"},
                         "positions") == {"data": []}
    with pytest.raises(broker.BrokerError):
        broker._check({"Error": [{"code": 424, "message": "Something else"}]}, "holdings")
    with pytest.raises(broker.BrokerError):
        broker._check({"Error": [{"code": 500, "message": "No service"}]}, "holdings")


def test_stat_not_ok_is_an_error():
    with pytest.raises(broker.BrokerError, match="place_order failed: Insufficient funds"):
        broker._check({"stat": "Not_Ok", "stCode": 1005, "errMsg": "Insufficient funds"}, "place_order")
    assert broker._check({"stat": "Ok", "nOrdNo": "1"}, "place_order") == {"stat": "Ok", "nOrdNo": "1"}


def test_non_dict_response_is_an_error():
    with pytest.raises(broker.BrokerError, match="unexpected response"):
        broker._check(["x"], "limits")


def test_kotak_check_cli(neo, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["main.py", "--kotak-check"])
    main.main()
    out = capsys.readouterr().out
    assert "Logged in as TEST USER" in out and "Holdings: 1 · positions: 0 · orders today: 2" in out
    saved = json.loads((tmp_path / "kotak_account_check.json").read_text(encoding="utf-8"))
    assert saved["limits"]["Net"] == "10000.00"
    assert "4321" not in out and SECRET not in out


def test_kotak_check_cli_missing_and_failed(neo, monkeypatch, capsys):
    monkeypatch.setattr(config, "KOTAK_MPIN", "")
    monkeypatch.setattr(sys, "argv", ["main.py", "--kotak-check"])
    main.main()
    assert "Missing in .env: KOTAK_MPIN" in capsys.readouterr().out
    monkeypatch.setattr(config, "KOTAK_MPIN", "4321")
    neo.fail_next = ["login"]
    main.main()
    assert "Kotak login / account read failed: TOTP login failed: Invalid TOTP" in capsys.readouterr().out


# ---- order calls: retry only after an auth refusal; explain "unauthorized" ------------------------

class OrderNeo(FakeNeo):
    place_results = []

    def place_order(self, **kw):
        self.calls.append(("place_order",))
        return OrderNeo.place_results.pop(0)

    def whatsmyip(self):
        return {"data": [{"ip": "203.0.113.7", "time": "x"}], "stCode": 1000}


@pytest.fixture
def order_neo(neo, monkeypatch):
    monkeypatch.setattr(neo_api_client, "NeoAPI", OrderNeo)
    return OrderNeo


def test_order_unauthorized_is_retried_once_then_explained(order_neo):
    order_neo.place_results = [{"Error": "unauthorized"}, {"Error": "unauthorized"}]
    with pytest.raises(broker.BrokerError) as e:
        broker.call("place_order", quantity="1")
    msg = str(e.value)
    assert "place_order failed: unauthorized" in msg and "203.0.113.7" in msg and "F&O segment" in msg
    assert sum(c[0] == "place_order" for inst in order_neo.instances for c in inst.calls) == 2


def test_order_auth_refusal_then_success(order_neo):
    order_neo.place_results = [{"Error": "unauthorized"}, {"nOrdNo": "9", "stat": "Ok"}]
    assert broker.call("place_order", quantity="1")["nOrdNo"] == "9"


def test_order_other_failure_is_not_retried(order_neo):
    # a timeout / server error may mean the order went through: never place it twice
    order_neo.place_results = [{"Error": "Server disconnected"}, {"nOrdNo": "DUPLICATE", "stat": "Ok"}]
    with pytest.raises(broker.BrokerError, match="Server disconnected"):
        broker.call("place_order", quantity="1")
    assert sum(c[0] == "place_order" for inst in order_neo.instances for c in inst.calls) == 1
    assert order_neo.place_results == [{"nOrdNo": "DUPLICATE", "stat": "Ok"}]


@pytest.mark.parametrize("msg,auth", [("unauthorized", True), ("Unauthorised access", True),
                                      ("Complete the 2fa process before accessing", True),
                                      ("Invalid session", True), ("RMS: insufficient funds", False),
                                      ("Server disconnected", False)])
def test_auth_error_detection(msg, auth):
    assert broker._auth_error(msg) is auth
