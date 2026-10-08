from __future__ import annotations
import logging

log=logging.getLogger(__name__)

SAS="m.sas.v1"

class SasVerifier:
    """Answers interactive emoji (SAS) verification from Element and similar clients.

    nio implements the SAS state machine (start/accept/key/mac) but not the request/ready/done
    wrapper that modern clients send around it, so that part lives here. A bot cannot look at
    the other screen, so it confirms the emoji itself; the human compares them with the log line.
    Only requests from `allowed_users` are answered.
    """
    def __init__(self,client,allowed_users):
        self.client=client; self.allowed=set(allowed_users)
    async def on_to_device(self,event):
        # Like _send, nothing here may escape into sync_forever.
        try: await self._dispatch(event)
        except Exception: log.exception("Key verification step failed")
    async def _dispatch(self,event):
        from nio import KeyVerificationStart, KeyVerificationKey, KeyVerificationMac, KeyVerificationCancel, UnknownToDeviceEvent
        if isinstance(event,UnknownToDeviceEvent):
            if event.type=="m.key.verification.request": await self._on_request(event)
            elif event.type=="m.key.verification.done": log.info("Verification %s: other side done",event.source["content"].get("transaction_id"))
        elif isinstance(event,KeyVerificationStart): await self._on_start(event)
        elif isinstance(event,KeyVerificationKey): await self._on_key(event)
        elif isinstance(event,KeyVerificationMac): await self._on_mac(event)
        elif isinstance(event,KeyVerificationCancel):
            log.info("Verification %s cancelled by %s: %s",event.transaction_id,event.sender,event.reason)
    async def _cancel(self,user,device,txn,code,reason):
        from nio.event_builders import ToDeviceMessage
        await self.client.to_device(ToDeviceMessage("m.key.verification.cancel",user,device,
            {"transaction_id":txn,"code":code,"reason":reason}))
    async def _on_request(self,event):
        c=event.source["content"]; txn=c.get("transaction_id"); dev=c.get("from_device")
        if not txn or not dev: return
        log.info("Verification request %s from %s %s",txn,event.sender,dev)
        if event.sender not in self.allowed:
            log.warning("Refusing verification from %s (not in matrix.verification_users)",event.sender)
            return await self._cancel(event.sender,dev,txn,"m.user","Verification not allowed")
        if SAS not in c.get("methods",[]):
            return await self._cancel(event.sender,dev,txn,"m.unknown_method","Only emoji verification is supported")
        # nio drops a start from a device it has no keys for, so fetch them before saying ready.
        if dev not in {d.id for d in self.client.device_store.active_user_devices(event.sender)}:
            self.client.users_for_key_query.add(event.sender)
            await self.client.keys_query()
        from nio.event_builders import ToDeviceMessage
        await self.client.to_device(ToDeviceMessage("m.key.verification.ready",event.sender,dev,
            {"from_device":self.client.device_id,"methods":[SAS],"transaction_id":txn}))
    async def _on_start(self,event):
        txn=event.transaction_id
        if event.sender not in self.allowed:
            log.warning("Refusing verification start from %s",event.sender)
            return await self._cancel(event.sender,event.from_device,txn,"m.user","Verification not allowed")
        if txn not in self.client.key_verifications:
            log.warning("Verification %s: nio rejected the start (unknown device or unsupported method)",txn); return
        await self.client.accept_key_verification(txn)
    async def _on_key(self,event):
        sas=self.client.key_verifications.get(event.transaction_id)
        if not sas or sas.canceled: return
        # nio queued our own key in reply; it must reach the other side before our MAC.
        await self.client.send_to_device_messages()
        emoji=" ".join(f"{e} ({name})" for e,name in sas.get_emoji())
        log.warning("Verification %s with %s %s — compare emoji: %s",
            event.transaction_id,event.sender,sas.other_olm_device.id,emoji)
        await self.client.confirm_short_auth_string(event.transaction_id)
    async def _on_mac(self,event):
        sas=self.client.key_verifications.get(event.transaction_id)
        if not sas: return
        if not sas.verified:
            log.warning("Verification %s failed: %s",event.transaction_id,sas.cancel_reason or "MAC mismatch"); return
        from nio.event_builders import ToDeviceMessage
        await self.client.to_device(ToDeviceMessage("m.key.verification.done",event.sender,sas.other_olm_device.id,
            {"transaction_id":event.transaction_id}))
        log.info("Verified device %s of %s",sas.other_olm_device.id,event.sender)
