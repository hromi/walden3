from types import SimpleNamespace
import pytest

nio = pytest.importorskip("nio")
from nio import UnknownToDeviceEvent, KeyVerificationStart, KeyVerificationKey, KeyVerificationMac

from walden.matrix_verification import SasVerifier


class FakeDeviceStore:
    def __init__(self, known):
        self.known = known

    def active_user_devices(self, user):
        return [SimpleNamespace(id=d) for d in self.known]


class FakeClient:
    def __init__(self, known=("ELEMENT",)):
        self.device_id = "BOTDEV"
        self.device_store = FakeDeviceStore(list(known))
        self.users_for_key_query = set()
        self.key_verifications = {}
        self.log = []

    async def to_device(self, msg):
        self.log.append((msg.type, msg.recipient, msg.recipient_device, msg.content))

    async def keys_query(self):
        self.log.append(("keys_query",))
        self.device_store.known.append("ELEMENT")

    async def accept_key_verification(self, txn):
        self.log.append(("accept", txn))

    async def send_to_device_messages(self):
        self.log.append(("flush",))

    async def confirm_short_auth_string(self, txn):
        self.log.append(("confirm", txn))


def request(sender, methods=("m.sas.v1",)):
    return UnknownToDeviceEvent({"sender": sender, "type": "m.key.verification.request", "content": {
        "from_device": "ELEMENT", "methods": list(methods), "transaction_id": "t1", "timestamp": 0}},
        sender, "m.key.verification.request")


def sas(verified=True):
    return SimpleNamespace(canceled=False, verified=verified, cancel_reason=None,
                           other_olm_device=SimpleNamespace(id="ELEMENT"),
                           get_emoji=lambda: [("🐶", "Dog"), ("🔑", "Key")])


@pytest.mark.asyncio
async def test_request_from_own_account_gets_ready():
    c = FakeClient()
    await SasVerifier(c, ["@walden:x"]).on_to_device(request("@walden:x"))
    assert c.log == [("m.key.verification.ready", "@walden:x", "ELEMENT",
                      {"from_device": "BOTDEV", "methods": ["m.sas.v1"], "transaction_id": "t1"})]


@pytest.mark.asyncio
async def test_request_from_unknown_device_queries_keys_first():
    c = FakeClient(known=())
    await SasVerifier(c, ["@walden:x"]).on_to_device(request("@walden:x"))
    assert c.log[0] == ("keys_query",) and c.log[1][0] == "m.key.verification.ready"
    assert "@walden:x" in c.users_for_key_query


@pytest.mark.asyncio
async def test_request_from_stranger_is_cancelled():
    c = FakeClient()
    await SasVerifier(c, ["@walden:x"]).on_to_device(request("@mallory:x"))
    assert c.log[0][0] == "m.key.verification.cancel" and c.log[0][3]["code"] == "m.user"


@pytest.mark.asyncio
async def test_request_without_sas_is_cancelled():
    c = FakeClient()
    await SasVerifier(c, ["@walden:x"]).on_to_device(request("@walden:x", methods=["m.qr_code.show.v1"]))
    assert c.log[0][3]["code"] == "m.unknown_method"


@pytest.mark.asyncio
async def test_start_key_mac_flow():
    c = FakeClient()
    v = SasVerifier(c, ["@walden:x"])
    c.key_verifications["t1"] = sas()
    start = SimpleNamespace(sender="@walden:x", from_device="ELEMENT", transaction_id="t1")
    await v._on_start(start)
    await v._on_key(SimpleNamespace(sender="@walden:x", transaction_id="t1"))
    await v._on_mac(SimpleNamespace(sender="@walden:x", transaction_id="t1"))
    assert [e[0] for e in c.log] == ["accept", "flush", "confirm", "m.key.verification.done"]
    assert c.log[-1][2] == "ELEMENT"


@pytest.mark.asyncio
async def test_failed_mac_sends_no_done():
    c = FakeClient()
    c.key_verifications["t1"] = sas(verified=False)
    await SasVerifier(c, ["@walden:x"])._on_mac(SimpleNamespace(sender="@walden:x", transaction_id="t1"))
    assert c.log == []


@pytest.mark.asyncio
async def test_errors_do_not_escape():
    c = FakeClient()
    async def boom(msg): raise RuntimeError("network")
    c.to_device = boom
    await SasVerifier(c, ["@walden:x"]).on_to_device(request("@walden:x"))
