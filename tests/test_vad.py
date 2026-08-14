from reachy_openai_realtime.vad import EnergyTurnDetector


def test_energy_vad_detects_voice_then_silence() -> None:
    vad = EnergyTurnDetector()

    for _ in range(10):
        assert not vad.process(-50.0, 20.0).started

    decisions = [vad.process(-35.0, 20.0) for _ in range(12)]
    assert decisions[-1].started
    assert vad.speech_active

    decisions = [vad.process(-52.0, 20.0) for _ in range(40)]
    assert decisions[-1].stopped
    assert decisions[-1].reason == "silence"
    assert not vad.speech_active


def test_energy_vad_does_not_start_on_stable_noise_floor() -> None:
    vad = EnergyTurnDetector()

    decisions = [vad.process(-49.5, 20.0) for _ in range(200)]

    assert not any(decision.started for decision in decisions)
    assert not vad.speech_active


def test_energy_vad_ignores_low_level_rustle() -> None:
    vad = EnergyTurnDetector()
    for _ in range(200):
        vad.process(-70.0, 20.0)

    decisions = [vad.process(-57.0, 20.0) for _ in range(30)]

    assert not any(decision.started for decision in decisions)
    assert not vad.speech_active


def test_hardware_speech_gate_rejects_loud_motor_noise() -> None:
    vad = EnergyTurnDetector()

    decisions = [
        vad.process(-25.0, 20.0, speech_detected=False) for _ in range(100)
    ]

    assert not any(decision.started for decision in decisions)
    assert not vad.speech_active


def test_hardware_speech_gate_accepts_human_voice() -> None:
    vad = EnergyTurnDetector()

    decisions = [
        vad.process(-35.0, 20.0, speech_detected=True) for _ in range(12)
    ]

    assert decisions[-1].started
    assert vad.speech_active


def test_energy_vad_forces_a_maximum_turn() -> None:
    vad = EnergyTurnDetector(min_speech_ms=20.0, max_turn_ms=100.0)

    assert vad.process(-30.0, 20.0).started
    decisions = [vad.process(-30.0, 20.0) for _ in range(4)]

    assert decisions[-1].stopped
    assert decisions[-1].reason == "maximum"
