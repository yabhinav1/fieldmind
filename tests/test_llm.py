"""The source check that guards generated answers. No language model is needed to run these."""

from fieldmind.llm import LocalModel, grounded

MOTOR = "Motor M-9 overheating, call the operator before restarting"
LIMITS = "Motor winding temperature limits: class F insulation alarms at 140 C and trips at 155 C."
GUIDE = "Vibration severity guide: below 2.8 mm/s is acceptable, above 7.1 mm/s is unacceptable."
PUMP = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"


def test_accepts_numbers_that_come_from_the_cited_note():
    answer = "No, 7.2 mm/s is unacceptable [2]. Above 7.1 mm/s the machine should be stopped [1]."
    assert grounded(answer, [GUIDE, PUMP], "is a vibration of 7.2 mm/s acceptable")


def test_rejects_a_manual_limit_reported_as_a_reading():
    # The motor note has no temperature; 140 C is a limit from the manual.
    answer = "The Motor M-9 is overheating, with a temperature reading of 140 C [1]. Check the cooling fans [2]."
    assert not grounded(answer, [MOTOR, LIMITS], "which machine is running too hot")


def test_rejects_invented_numbers_and_citations():
    assert not grounded("The pump vibration is 9.4 mm/s [1].", [PUMP])
    assert not grounded("The pump vibration is 7.2 mm/s [3].", [PUMP, GUIDE])
    assert not grounded("Replace it within 30 days.", [PUMP, GUIDE])


def test_uncited_sentence_may_use_any_retrieved_number():
    assert grounded("The limit is 7.1 mm/s and the pump is at 7.2 mm/s.", [GUIDE, PUMP])
    assert grounded("The device has no record of it.", [GUIDE, PUMP])


def test_model_is_skipped_cleanly_when_not_running():
    model = LocalModel("http://127.0.0.1:9", "llama3.2:3b")  # nothing listens on port 9
    assert model.available() is False
    assert model.answer("anything", []) == (None, None)
    assert LocalModel("http://127.0.0.1:9", "").available() is False


def test_ask_falls_back_to_the_notes_without_a_model(fleet):
    a = fleet("edge-a")
    a.service.capture(PUMP)
    answer = a.service.ask("what is wrong with pump P-102")
    assert answer["engine"] == "device memory" and "7.2 mm/s" in answer["answer"]
    assert answer["note"] is None
