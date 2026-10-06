from uplink.rf_hunter import EventStore, RfEvent
from uplink.rf_transport import RfBleTransport, decode_frame, encode_frame
import hashlib


def event():
    return RfEvent("device", "session", 1, "2026-01-01T00:00:00Z", 1, frequency_hz=433920000)


def test_import_resume_ack_and_dedupe(tmp_path):
    store = EventStore(tmp_path)
    transport = RfBleTransport(store, chunk_size=2)
    e = event(); payload = b"abcd"
    assert transport.receive({"v": 1, "op": "event_begin", "event_id": e.event_id, "metadata": e.to_dict()})["offset"] == 0
    assert transport.receive({"v": 1, "op": "event_chunk", "event_id": e.event_id, "offset": 0, "data": "YWI="})["offset"] == 2
    assert transport.receive({"v": 1, "op": "event_resume", "event_id": e.event_id})["offset"] == 2
    transport.receive({"v": 1, "op": "event_chunk", "event_id": e.event_id, "offset": 2, "data": "Y2Q="})
    assert transport.receive({"v": 1, "op": "event_commit", "event_id": e.event_id, "sha256": hashlib.sha256(payload).hexdigest()})["op"] == "ack"
    assert transport.receive({"v": 1, "op": "event_begin", "event_id": e.event_id, "metadata": e.to_dict()})["duplicate"]


def test_upload_frames_are_framed_and_resumable(tmp_path):
    store = EventStore(tmp_path); e = event(); store.add(e, b"abcd")
    t = RfBleTransport(store, chunk_size=2)
    frames = list(t.iter_upload_bytes())
    assert decode_frame(frames[0])["op"] == "event_begin"
    assert [decode_frame(x)["offset"] for x in frames if decode_frame(x)["op"] == "event_chunk"] == [0, 2]
