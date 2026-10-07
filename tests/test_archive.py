"""归档流水线单测：producer / worker / indexing_worker。

约定：
  * 不需要真实 mp4——用手拼的最小 MP4（ftyp+moov+mdat 头）测试拷贝；
  * SEI 解析统一走可注入的桩（sys.modules 注入 ``web.services.sei_parser``，
    或 monkeypatch 模块函数），绝不依赖并行创建中的真实解析器；
  * 队列 DB 一律用 tmp_path 隔离，不碰仓库 state/。
"""

import json
import logging
import os
import sys
import types
from datetime import datetime, timezone

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.services import archive_producer as ap
from web.services import archive_worker as aw
from web.services import indexing_worker as iw


# ---------------------------------------------------------------------------
# 固件
# ---------------------------------------------------------------------------

def _box(typ: bytes, payload: bytes = b'') -> bytes:
    size = 8 + len(payload)
    return size.to_bytes(4, 'big') + typ + payload


def _mini_mp4(payload_size: int = 1024) -> bytes:
    """最小可校验 MP4：ftyp + moov + mdat（仅 box 头 + 填充）。"""
    return (
        _box(b'ftyp', b'isom' + b'\x00' * 12)
        + _box(b'moov')
        + _box(b'mdat', b'\x00' * payload_size)
    )


@pytest.fixture()
def teslacam(tmp_path):
    root = tmp_path / 'TeslaCam'
    (root / 'RecentClips').mkdir(parents=True)
    (root / 'SentryClips' / '2026-10-07_09-15-32').mkdir(parents=True)
    (root / 'SavedClips' / '2026-10-07_08-00-00').mkdir(parents=True)
    return root


def _write_clip(path, *, old=True, payload_size=1024):
    path = str(path)
    with open(path, 'wb') as f:
        f.write(_mini_mp4(payload_size))
    if old:
        # 足够旧：绕过写稳定门与 peek 新鲜度门
        ancient = 1_700_000_000.0
        os.utime(path, (ancient, ancient))
    return path


@pytest.fixture()
def archive_db(tmp_path):
    return str(tmp_path / 'archive_queue.db')


@pytest.fixture()
def index_db(tmp_path):
    return str(tmp_path / 'indexing.db')


@pytest.fixture(autouse=True)
def _reset_producer_state():
    ap.reset_peek_cache()
    ap.reset_skipped_stationary_tally()
    # 统计清零（reset_peek_cache 只清条目）
    for k in ap._peek_cache_stats:
        ap._peek_cache_stats[k] = 0
    yield
    ap.stop_producer(timeout=5)
    aw.stop_worker(timeout=5)
    iw.stop_worker(timeout=5)
    ap.reset_peek_cache()
    ap.reset_skipped_stationary_tally()


def _fake_sei_module(monkeypatch, **overrides):
    """向 sys.modules 注入桩 sei_parser。"""
    mod = types.ModuleType('web.services.sei_parser')

    def _write_sidecar(dest_path, sample_rate=30):
        p = dest_path + '.sei.json'
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'sei_count': 3, 'sample_rate': sample_rate}, f)

        class _Sidecar:
            sei_count = 3
            messages = []
            no_movement_count = 3
            mvhd_creation_time_utc = None

        return _Sidecar()

    mod.write_sei_sidecar = _write_sidecar
    mod.read_sei_sidecar = lambda path: None

    def _extract(path, sample_rate=30, max_walk_bytes=None):
        return iter([])

    mod.extract_sei_messages = _extract
    for k, v in overrides.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, 'web.services.sei_parser', mod)
    return mod


# ---------------------------------------------------------------------------
# producer：写稳定等待 / peek 缓存 / 解析失败跳过
# ---------------------------------------------------------------------------

def test_stable_write_age_logic(archive_db):
    """_stable_write_age_seconds_for：RecentClips 取 max(5, 90)。"""
    recent = {'priority': aw.PRIORITY_RECENT_CLIPS}
    event = {'priority': aw.PRIORITY_EVENTS}
    assert aw._stable_write_age_seconds_for(recent) == max(
        aw._stable_write_age_seconds(),
        aw._recent_clips_stable_write_age_seconds(),
    )
    assert aw._stable_write_age_seconds_for(event) == \
        aw._stable_write_age_seconds()
    assert aw._stable_write_age_seconds_for(recent) >= 90.0


def test_producer_stable_age_skips_peek_for_fresh_file(
        teslacam, archive_db, monkeypatch):
    """太新的 RecentClips 直接入队、不做 SEI peek（交给 worker 稳定门）。"""
    calls = []
    monkeypatch.setattr(
        ap, '_peek_clip_for_movement',
        lambda p: calls.append(p) or True,
    )
    monkeypatch.setattr(ap, '_stable_write_age_seconds',
                        lambda: 10_000.0)
    fresh = _write_clip(teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4',
                        old=False)
    result = ap.enqueue_with_peek([fresh], db_path=archive_db)
    assert calls == [], "新鲜文件不应触发 SEI peek"
    assert result['enqueued'] == 1


def test_peek_cache_hit(teslacam, archive_db, monkeypatch):
    """第二次扫描命中 peek 缓存，不再重复解析文件。"""
    calls = []
    monkeypatch.setattr(
        ap, '_peek_clip_for_movement',
        lambda p: calls.append(p) or False,  # 静止 → 跳过
    )
    clip = _write_clip(
        teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4')

    first = ap.enqueue_with_peek([clip], db_path=archive_db)
    second = ap.enqueue_with_peek([clip], db_path=archive_db)

    assert first['skipped_stationary'] == 1
    assert second['skipped_stationary'] == 1
    assert first['enqueued'] == 0 and second['enqueued'] == 0
    assert len(calls) == 1, "第二次应命中缓存，不再 peek"
    stats = ap.get_peek_cache_stats()
    assert stats['hits'] >= 1
    assert stats['misses'] >= 1


def test_producer_skips_on_sei_parse_failure(
        teslacam, archive_db, monkeypatch, caplog):
    """SEI 解析失败（None）：跳过、记中文日志、不崩溃、不入队。"""
    monkeypatch.setattr(ap, '_peek_clip_for_movement', lambda p: None)
    clip = _write_clip(
        teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4')

    with caplog.at_level(logging.WARNING, logger='web.services.archive_producer'):
        result = ap.enqueue_with_peek([clip], db_path=archive_db)

    assert result['enqueued'] == 0
    assert result['considered'] == 1
    assert 'SEI 解析失败' in caplog.text
    assert aw.get_queue_status(archive_db).get('pending', 0) == 0


def test_boot_catchup_once(teslacam, archive_db, monkeypatch):
    """开机补扫：事件片段直接入队，RecentClips 走 peek。"""
    monkeypatch.setattr(ap, '_peek_clip_for_movement', lambda p: True)
    sentry = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    recent = _write_clip(
        teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4')

    result = ap.run_boot_catchup_once(str(teslacam), archive_db)
    assert result['seen'] == 2
    assert result['enqueued'] == 2
    assert sentry and recent


def test_producer_lifecycle(teslacam, archive_db):
    """生产者启停幂等。"""
    assert ap.start_producer(str(teslacam), archive_db,
                             rescan_interval_seconds=100,
                             boot_catchup_enabled=False) is True
    assert ap.start_producer(str(teslacam), archive_db) is False
    assert ap.stop_producer(timeout=5) is True
    assert ap.get_producer_status()['running'] is False


# ---------------------------------------------------------------------------
# worker：入队/认领往返、原子拷贝 + sidecar、失败路径
# ---------------------------------------------------------------------------

def test_enqueue_dequeue_roundtrip(archive_db, teslacam):
    src = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    assert aw.enqueue_for_archive(src, db_path=archive_db) is True
    # 幂等：重复入队不新增
    assert aw.enqueue_for_archive(src, db_path=archive_db) is False

    row = aw.claim_next_for_worker('test-worker', db_path=archive_db)
    assert row is not None
    assert row['source_path'] == src
    assert row['priority'] == aw.PRIORITY_EVENTS

    assert aw.mark_copied(row['id'], '/tmp/dest.mp4',
                          db_path=archive_db) is True
    status = aw.get_queue_status(archive_db)
    assert status.get('copied', 0) == 1
    assert status.get('pending', 0) == 0


def test_claim_priority_order(archive_db, teslacam):
    """事件片段（P1）先于 RecentClips（P2）被认领。"""
    recent = _write_clip(
        teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4')
    sentry = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    aw.enqueue_many_for_archive([recent, sentry], db_path=archive_db)
    first = aw.claim_next_for_worker('w', db_path=archive_db)
    assert first['source_path'] == sentry


def test_mark_failed_and_dead_letter(archive_db, teslacam):
    src = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    aw.enqueue_for_archive(src, db_path=archive_db)
    row = aw.claim_next_for_worker('w', db_path=archive_db)
    assert aw.mark_failed(row['id'], 'boom', max_attempts=2,
                          db_path=archive_db) == 'pending'
    row2 = aw.claim_next_for_worker('w', db_path=archive_db)
    assert aw.mark_failed(row2['id'], 'boom', max_attempts=2,
                          db_path=archive_db) == 'dead_letter'


def test_worker_atomic_copy_with_sidecar(
        teslacam, tmp_path, archive_db, monkeypatch):
    """worker 原子拷贝：目标落盘、内容一致、SEI sidecar 生成、送入索引队列。"""
    _fake_sei_module(monkeypatch)
    indexed_calls = []
    monkeypatch.setattr(aw, '_enqueue_indexed',
                        lambda dest: indexed_calls.append(dest))

    archive_root = str(tmp_path / 'ArchivedClips')
    src = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    aw.enqueue_for_archive(src, db_path=archive_db)
    row = aw.claim_next_for_worker('w', db_path=archive_db)

    outcome = aw.process_one_claim(
        row, archive_db, archive_root, str(teslacam),
        chunk_size=4096, max_attempts=3,
    )

    assert outcome == 'copied'
    dest = os.path.join(
        archive_root, 'SentryClips', '2026-10-07_09-15-32',
        '2026-10-07_09-15-32-front.mp4')
    assert os.path.isfile(dest)
    with open(src, 'rb') as f1, open(dest, 'rb') as f2:
        assert f1.read() == f2.read()
    # staging 里没有残留 partial
    staging = os.path.join(archive_root, '.staging')
    leftovers = [n for n in os.listdir(staging)] if os.path.isdir(staging) \
        else []
    assert leftovers == []
    # sidecar JSON 已生成（含 SEI 摘要）
    sidecar_p = dest + '.sei.json'
    assert os.path.isfile(sidecar_p)
    with open(sidecar_p, encoding='utf-8') as f:
        sidecar_data = json.load(f)
    assert sidecar_data['schema_version'] == 1
    assert sidecar_data['video_size_bytes'] == os.path.getsize(dest)
    # 索引侧能读回 sidecar
    assert iw._read_sei_sidecar(dest) == []
    # 索引入队钩子被调用
    assert indexed_calls == [dest]
    # 行状态
    assert aw.get_queue_status(archive_db).get('copied', 0) == 1


def test_worker_stable_write_gate_defers(tmp_path, archive_db):
    """源文件在写（mtime 新鲜且元数据漂移）→ 释放回 pending。"""
    src = str(tmp_path / 'clip.mp4')
    with open(src, 'wb') as f:
        f.write(_mini_mp4())
    ancient = 1_700_000_000.0
    os.utime(src, (ancient, ancient))
    aw.enqueue_for_archive(src, db_path=archive_db)
    # 入队后继续写：mtime 变新、size 变化
    with open(src, 'ab') as f:
        f.write(b'\x00' * 512)

    row = aw.claim_next_for_worker('w', db_path=archive_db)
    outcome = aw.process_one_claim(
        row, archive_db, str(tmp_path / 'arch'), None,
        chunk_size=4096, max_attempts=3,
    )
    assert outcome == 'pending'
    assert aw.get_queue_status(archive_db).get('pending', 0) == 1


def test_worker_skips_stationary_recent(
        teslacam, tmp_path, archive_db, monkeypatch):
    """RecentClips 无移动信号 → skipped_stationary，不拷贝。"""
    _fake_sei_module(monkeypatch)
    monkeypatch.setattr(aw, '_clip_has_movement', lambda p: False)
    monkeypatch.setattr(aw, '_enqueue_indexed',
                        lambda dest: pytest.fail("静止片段不应入索引队列"))

    src = _write_clip(
        teslacam / 'RecentClips' / '2026-10-07_09-00-00-front.mp4')
    aw.enqueue_for_archive(src, db_path=archive_db)
    row = aw.claim_next_for_worker('w', db_path=archive_db)

    outcome = aw.process_one_claim(
        row, archive_db, str(tmp_path / 'arch'), str(teslacam),
        chunk_size=4096, max_attempts=3,
    )
    assert outcome == 'skipped_stationary'
    assert aw.get_queue_status(archive_db).get('skipped_stationary', 0) == 1


def test_sidecar_roundtrip_with_movement(tmp_path, monkeypatch):
    """sidecar 往返：worker 写入移动消息 → 索引侧读回可用。"""
    msg = types.SimpleNamespace(
        frame_index=30, timestamp_ms=1000.0,
        vehicle_speed_mps=12.5,
        linear_acceleration_x=-8.5,
        linear_acceleration_y=0.2,
        linear_acceleration_z=0.1,
        steering_wheel_angle=0.0,
        accelerator_pedal_position=0.0,
        brake_applied=True,
        gear_state='DRIVE', autopilot_state='NONE',
        blinker_on_left=False, blinker_on_right=False,
        has_movement=True,
    )
    mod = types.ModuleType('web.services.sei_parser')
    mod.extract_sei_messages = lambda p, sample_rate=30: iter([msg])
    monkeypatch.setitem(sys.modules, 'web.services.sei_parser', mod)

    dest = _write_clip(tmp_path / 'clip.mp4')
    path = aw._write_inline_sei_sidecar(dest)
    assert path == dest + '.sei.json'

    back = iw._read_sei_sidecar(dest)
    assert len(back) == 1
    assert back[0].vehicle_speed_mps == 12.5
    assert back[0].brake_applied is True
    assert back[0].gear_state == 'DRIVE'

    # 文件变化后 sidecar 失效
    with open(dest, 'ab') as f:
        f.write(b'\x00')
    assert iw._read_sei_sidecar(dest) is None


def test_worker_source_gone(tmp_path, archive_db):
    src = str(tmp_path / 'gone.mp4')
    with open(src, 'wb') as f:
        f.write(_mini_mp4())
    aw.enqueue_for_archive(src, db_path=archive_db)
    os.remove(src)
    row = aw.claim_next_for_worker('w', db_path=archive_db)
    outcome = aw.process_one_claim(
        row, archive_db, str(tmp_path / 'arch'), None,
        chunk_size=4096, max_attempts=3,
    )
    assert outcome == 'source_gone'


def test_recover_stale_claims(archive_db, teslacam):
    src = _write_clip(
        teslacam / 'SentryClips' / '2026-10-07_09-15-32'
        / '2026-10-07_09-15-32-front.mp4')
    aw.enqueue_for_archive(src, db_path=archive_db)
    row = aw.claim_next_for_worker('dead-worker', db_path=archive_db)
    assert row is not None
    # 立刻回收（max_age_seconds=0）
    assert aw.recover_stale_claims(archive_db, max_age_seconds=0) == 1
    assert aw.get_queue_status(archive_db).get('pending', 0) == 1


# ---------------------------------------------------------------------------
# indexing_worker：事件提取、process_claimed_item、pause/resume
# ---------------------------------------------------------------------------

def _msg(**kw):
    """SEI 消息桩。"""
    base = dict(
        frame_index=0, timestamp_ms=0.0,
        vehicle_speed_mps=0.0,
        linear_acceleration_x=0.0,
        linear_acceleration_y=0.0,
        linear_acceleration_z=0.0,
        brake_applied=False,
        has_movement=False,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_detect_motion_events():
    base = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
    msgs = [
        _msg(linear_acceleration_x=-8.5, timestamp_ms=1000.0, frame_index=30),
        _msg(linear_acceleration_x=-5.0, timestamp_ms=2000.0, frame_index=60),
        _msg(linear_acceleration_x=4.2, timestamp_ms=3000.0, frame_index=90),
        _msg(linear_acceleration_y=5.1, timestamp_ms=4000.0, frame_index=120),
    ]
    events = iw._detect_motion_events(
        msgs, base, '/v/clip.mp4', iw._thresholds())
    kinds = [e['event_type'] for e in events]
    assert kinds == ['emergency_brake', 'harsh_brake',
                     'hard_acceleration', 'sharp_turn']
    assert '急刹车' in events[0]['description']
    # 坐标恒为 None（未知≠0.0）
    assert all(e['lat'] is None and e['lon'] is None for e in events)


def test_debounce_events():
    base = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
    msgs = [
        _msg(linear_acceleration_x=-8.5, timestamp_ms=1000.0),
        _msg(linear_acceleration_x=-9.0, timestamp_ms=1500.0),  # 5s 内同类去重
        _msg(linear_acceleration_x=-8.0, timestamp_ms=9000.0),  # 窗口外保留
    ]
    events = iw._detect_motion_events(
        msgs, base, '/v/clip.mp4', iw._thresholds())
    assert [e['event_type'] for e in events] == [
        'emergency_brake', 'emergency_brake']


def test_sentry_event_from_event_json(tmp_path):
    """哨兵事件：event.json 的原因与坐标优先；缺失时 lat/lon 为 None。"""
    folder = tmp_path / 'SentryClips' / '2026-10-07_09-15-32'
    folder.mkdir(parents=True)
    (folder / 'event.json').write_text(json.dumps({
        'reason': 'sentry_aware_object_detection',
        'est_lat': 31.23, 'est_lon': 121.47,
    }), encoding='utf-8')
    clip = str(folder / '2026-10-07_09-15-32-front.mp4')
    base = datetime(2026, 10, 7, 9, 15, 32, tzinfo=timezone.utc)

    events = iw._sentry_folder_events(
        clip, str(tmp_path), [_msg()], base)
    kinds = [e['event_type'] for e in events]
    assert 'sentry' in kinds
    sentry = next(e for e in events if e['event_type'] == 'sentry')
    assert sentry['lat'] == 31.23 and sentry['lon'] == 121.47
    assert 'sentry_aware_object_detection' in sentry['description']

    # 无 event.json：坐标为 None，不崩溃
    folder2 = tmp_path / 'SentryClips' / '2026-10-07_10-00-00'
    folder2.mkdir(parents=True)
    events2 = iw._sentry_folder_events(
        str(folder2 / 'x-front.mp4'), str(tmp_path), [_msg()], base)
    sentry2 = next(e for e in events2 if e['event_type'] == 'sentry')
    assert sentry2['lat'] is None and sentry2['lon'] is None


def test_sentry_trigger_on_movement(tmp_path):
    """哨兵片段 SEI 显示移动 → sentry_trigger（基于速度/踏板，不依赖 GPS）。"""
    folder = tmp_path / 'SentryClips' / '2026-10-07_09-15-32'
    folder.mkdir(parents=True)
    clip = str(folder / '2026-10-07_09-15-32-front.mp4')
    base = datetime(2026, 10, 7, 9, 15, 32, tzinfo=timezone.utc)
    events = iw._sentry_folder_events(
        clip, str(tmp_path),
        [_msg(has_movement=True, vehicle_speed_mps=2.0)], base)
    kinds = [e['event_type'] for e in events]
    assert 'sentry_trigger' in kinds


def _claimed_index_row(index_db, file_path):
    iw.enqueue_for_indexing(index_db, file_path, source='archive')
    row = iw.claim_next_queue_item(index_db, 'test-indexer')
    assert row is not None
    return row


def test_process_claimed_item_complete(index_db, tmp_path):
    clip = str(tmp_path / 'clip.mp4')
    with open(clip, 'wb') as f:
        f.write(_mini_mp4())
    row = _claimed_index_row(index_db, clip)

    def _fake_indexer(path, db, tc):
        return iw.IndexResult(iw.IndexOutcome.INDEXED)

    action = iw.process_claimed_item(row, index_db, None,
                                     indexer=_fake_indexer)
    assert action.action == 'complete'
    assert action.outcome == iw.IndexOutcome.INDEXED
    iw._apply_action(action, row, index_db)
    assert iw.get_queue_status(index_db)['queue_ready'] == 0


def test_process_claimed_item_parse_error_defers(index_db, tmp_path):
    clip = str(tmp_path / 'clip.mp4')
    with open(clip, 'wb') as f:
        f.write(_mini_mp4())
    row = _claimed_index_row(index_db, clip)

    def _fake_indexer(path, db, tc):
        return iw.IndexResult(iw.IndexOutcome.PARSE_ERROR, '解析炸了')

    action = iw.process_claimed_item(row, index_db, None,
                                     indexer=_fake_indexer)
    assert action.action == 'defer'
    assert action.bump_attempts is True
    assert action.next_attempt_at > 0
    iw._apply_action(action, row, index_db)
    status = iw.get_queue_status(index_db)
    assert status['queue_scheduled'] == 1


def test_process_claimed_item_indexer_exception(index_db, tmp_path):
    """indexer 抛异常 → 转为退避 defer，线程不死。"""
    clip = str(tmp_path / 'clip.mp4')
    with open(clip, 'wb') as f:
        f.write(_mini_mp4())
    row = _claimed_index_row(index_db, clip)

    def _boom(path, db, tc):
        raise RuntimeError('boom')

    action = iw.process_claimed_item(row, index_db, None, indexer=_boom)
    assert action.action == 'defer'
    assert action.bump_attempts is True


def test_index_single_file_end_to_end(index_db, tmp_path, monkeypatch):
    """_index_single_file：SEI 桩 → 事件入库 → indexed_files 标记。"""
    _fake_sei_module(monkeypatch)
    clip = _write_clip(tmp_path / '2026-10-07_09-00-00-front.mp4')
    monkeypatch.setattr(
        iw, '_iter_sei_messages',
        lambda p: [_msg(linear_acceleration_x=-8.5, timestamp_ms=500.0)],
    )
    result = iw._index_single_file(clip, index_db, None)
    assert result.outcome == iw.IndexOutcome.INDEXED

    status = iw.get_queue_status(index_db)
    assert status['events_total'] == 1
    assert status['indexed_files_total'] == 1

    # 再次索引同一文件 → ALREADY_INDEXED
    result2 = iw._index_single_file(clip, index_db, None)
    assert result2.outcome == iw.IndexOutcome.ALREADY_INDEXED


def test_indexing_pause_resume(index_db, monkeypatch):
    """索引 worker 启停/暂停/恢复（播放时后台让路）。"""
    monkeypatch.setattr(iw, '_IDLE_SLEEP_SECONDS', 0.05)
    monkeypatch.setattr(iw, '_INTER_FILE_SLEEP_SECONDS', 0.05)

    assert iw.start_worker(index_db, None) is True
    assert iw.start_worker(index_db, None) is False  # 幂等
    assert iw.is_running() is True

    assert iw.pause_worker(timeout=5) is True
    assert iw.is_paused() is True

    iw.resume_worker()
    assert iw.is_paused() is False

    assert iw.stop_worker(timeout=5) is True
    assert iw.is_running() is False
