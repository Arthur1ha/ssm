"""Teaching observation budgets use elapsed monotonic time, without sleeping."""

import pytest

from cloud.go2.perception.observation_schedule import TargetObservationSchedule


def test_first_check_and_changed_scene_share_one_request_and_start_interval():
    schedule = TargetObservationSchedule()
    assert schedule.due(100)
    schedule.start(100)
    assert not schedule.due(101, changed=True, refresh=True)
    schedule.succeed(102.5)
    assert not schedule.due(102.999, changed=True)
    assert schedule.due(103, changed=True)
    schedule.start(103)
    assert not schedule.due(200, changed=True, refresh=True)


def test_static_fallback_is_counted_from_successful_analysis_completion():
    schedule = TargetObservationSchedule()
    schedule.start(100)
    schedule.succeed(102)
    assert not schedule.due(130)
    assert not schedule.due(131.999)
    assert schedule.due(132)
    schedule.start(132)
    schedule.succeed(133)
    assert not schedule.due(162.999)
    assert schedule.due(163)


@pytest.mark.parametrize('reason', ['change', 'freshness', 'new_teaching'])
def test_on_demand_checks_do_not_wait_for_static_fallback(reason):
    schedule = TargetObservationSchedule()
    schedule.start(10)
    schedule.succeed(11)
    options = {'changed': reason == 'change', 'refresh': reason == 'freshness'}
    if reason == 'new_teaching':
        schedule.request()
    assert not schedule.due(12.9, **options)
    assert schedule.due(13, **options)


def test_failed_analysis_backs_off_even_when_scene_changes_or_refresh_is_urgent():
    schedule = TargetObservationSchedule()
    schedule.start(10)
    schedule.succeed(11)
    schedule.start(14)
    schedule.fail(16)
    schedule.request()
    assert not schedule.due(25.999, changed=True, refresh=True)
    assert schedule.due(26)
    schedule.start(26)
    assert not schedule.due(27, changed=True)
    schedule.succeed(28)
    assert not schedule.due(29)
    assert schedule.due(29, refresh=True)


def test_failure_does_not_postpone_the_last_successful_analysis_deadline():
    schedule = TargetObservationSchedule()
    schedule.start(100)
    schedule.succeed(101)
    schedule.start(125)
    schedule.fail(126)
    assert schedule.last_success == 101
    assert not schedule.due(131, refresh=True)
    assert schedule.due(136)


def test_new_teaching_during_an_inflight_analysis_is_not_lost_at_completion():
    schedule = TargetObservationSchedule()
    schedule.start(100)
    schedule.request()
    assert not schedule.due(101)
    schedule.succeed(102)
    assert not schedule.due(102.999)
    assert schedule.due(103)
    schedule.start(103)
    schedule.succeed(104)
    assert not schedule.due(106)


def test_cancellation_does_not_reset_start_spacing_or_failure_backoff():
    schedule = TargetObservationSchedule()
    schedule.start(100)
    schedule.cancel()
    schedule.request()
    assert not schedule.due(102.999)
    assert schedule.due(103)
    schedule.start(103)
    schedule.fail(104)
    schedule.cancel()
    schedule.request()
    assert not schedule.due(113.999, changed=True, refresh=True)
    assert schedule.due(114)


@pytest.mark.parametrize('value', ['', 'invalid', 'nan', 'inf', '-1', '0'])
def test_invalid_environment_durations_keep_the_documented_defaults(monkeypatch, value):
    for name in ('GO2_TRAINING_VISION_MIN_INTERVAL_S', 'GO2_TRAINING_VISION_FALLBACK_S',
                 'GO2_TRAINING_VISION_RETRY_S'):
        monkeypatch.setenv(name, value)
    schedule = TargetObservationSchedule.from_environment()
    assert schedule.min_interval_s == 3
    assert schedule.fallback_s == 30
    assert schedule.retry_s == 10


def test_environment_can_select_explicit_observation_budgets(monkeypatch):
    monkeypatch.setenv('GO2_TRAINING_VISION_MIN_INTERVAL_S', '4.5')
    monkeypatch.setenv('GO2_TRAINING_VISION_FALLBACK_S', '45')
    monkeypatch.setenv('GO2_TRAINING_VISION_RETRY_S', '12')
    schedule = TargetObservationSchedule.from_environment()
    schedule.start(100)
    schedule.succeed(101)
    assert not schedule.due(104.49, changed=True)
    assert schedule.due(104.5, changed=True)
    assert not schedule.due(145.99)
    assert schedule.due(146)
