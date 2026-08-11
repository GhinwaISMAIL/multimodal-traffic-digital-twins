from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_receiver_deadlines_are_removed_from_executed_scripts():
    runner = (ROOT / "deploy_ric5g.sh").read_text()

    assert 'stage_receiver "$SCRIPTS/dn_ul_rx.mgn" "$LOGS/dn_ul_rx.mgn"' in runner
    assert 'stage_receiver "$SCRIPTS/ue${n}_dl_rx.mgn" "$LOGS/ue${n}_dl_rx.mgn"' in runner
    assert 'fields[1].upper() == "IGNORE"' in runner
    assert 'push "$CORE_HOST" "$DN_CONTAINER" "$LOGS/dn_ul_rx.mgn"' in runner
    assert 'push "$h" "ric5g-ue-cell$c-$u" "$LOGS/ue${n}_dl_rx.mgn"' in runner


def test_receivers_start_after_slow_preflight_checks():
    runner = (ROOT / "deploy_ric5g.sh").read_text()

    channel = runner.index('echo "== 7a/9 verify runtime channel control =="')
    ric = runner.index('echo "== 7b/9 verify RIC is ready =="')
    clocks = runner.index('echo "== 7c/9 prepare node clocks =="')
    receivers = runner.index('echo "== 7e/9 arm receivers =="')
    senders = runner.index('echo "== 8/9 start senders =="')

    assert channel < receivers
    assert ric < receivers
    assert clocks < receivers < senders
