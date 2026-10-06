import pytest

from fieldmind.policy import PolicyEngine, find_asset


@pytest.fixture(scope="module")
def policy(embedder, names):
    return PolicyEngine(embedder, names)


@pytest.mark.parametrize("text, scope, category", [
    ("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement", "shared", "equipment_fault"),
    ("Smell of gas near compressor K-4, isolated the line and cleared the bay", "shared", "safety_hazard"),
    ("Replaced gasket on valve V-17 and torqued flange bolts to 85 Nm", "shared", "procedure_fix"),
    ("Technician Ravi Kumar reported chest pain during night shift, sent to clinic", "private", "personal_health"),
    ("Remind me to call home after shift and pick up my keys", "private", "scratch_note"),
    ("Supervisor was rude to the new trainee again today", "private", "people_hr"),
])
def test_scope_follows_meaning(policy, text, scope, category):
    decision = policy.decide(text)
    assert (decision.scope, decision.category) == (scope, category)
    assert decision.reasons


def test_credentials_never_leave_even_when_asked(policy):
    text = "SCADA panel login password is Plant@2026"
    assert policy.decide(text).scope == "private"
    assert policy.decide(text, override="shared").scope == "private"


def test_useful_note_with_personal_details_is_masked(policy):
    decision = policy.decide("Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting")
    assert decision.scope == "redacted"
    assert "Anil" not in decision.shared_text and "9876543210" not in decision.shared_text
    assert "Motor M-9 overheating" in decision.shared_text


def test_stored_policy_summary_holds_no_matched_values(policy):
    summary = policy.decide("Conveyor C-3 drifting, contact vendor at support@beltco.in").summary()
    assert summary["signals"] == ["email"]
    assert "beltco" not in str(summary)


def test_safety_notes_are_urgent(policy):
    assert policy.decide("Exposed live cable found behind panel MCC-2, area barricaded").priority == 2


def test_user_can_keep_a_shareable_note_private(policy):
    decision = policy.decide("Pump P-102 bearing vibration high", override="private")
    assert decision.scope == "private" and decision.decided_by == "user"


def test_sharing_override_still_masks_personal_details(policy):
    decision = policy.decide("Technician Ravi Kumar reported chest pain, sent to clinic", override="shared")
    assert decision.scope == "redacted"
    assert "Ravi" not in decision.shared_text


def test_asset_tags_are_extracted():
    assert find_asset("Pump P-102 bearing vibration high at 7.2 mm/s") == "P-102"
    assert find_asset("winding temperature 96C") is None


@pytest.mark.parametrize("text, hidden", [
    ("Suresh fixed pump P-102 seal yesterday", ["Suresh"]),
    ("Motor M-9 tripped twice, Anil restarted it both times", ["Anil"]),
    ("Spoke with Deepak Yadav about the K-4 compressor drain valve", ["Deepak", "Yadav"]),
    ("Mohammed and Lakshmi replaced the gasket on HX-5", ["Mohammed", "Lakshmi"]),
    ("Asked Priya to check valve V-17 before the shift ends", ["Priya"]),
])
def test_bare_names_are_masked_without_a_title(policy, text, hidden):
    decision = policy.decide(text)
    assert decision.scope == "redacted"
    for name in hidden:
        assert name not in decision.shared_text
    assert "[person removed]" in decision.shared_text


@pytest.mark.parametrize("text, signal, hidden", [
    ("Forklift KA 01 AB 1234 hydraulic hose burst near bay 3", "vehicle", "KA 01 AB 1234"),
    ("Contractor with badge #A1234 left the MCC-2 panel open", "badge", "A1234"),
    ("Spare parts delivered to Flat 12, 3rd Cross, Indiranagar for the V-17 job", "address", "Flat 12, 3rd Cross"),
    ("Emp id 48213 reported the boiler B-2 pressure at 6.1 bar", "badge", "48213"),
])
def test_vehicles_badges_and_addresses_are_masked(policy, text, signal, hidden):
    decision = policy.decide(text)
    assert signal in {s["type"] for s in decision.signals}, decision.signals
    assert decision.scope == "redacted" and hidden not in decision.shared_text


def test_readings_and_asset_tags_are_not_mistaken_for_identifiers(policy):
    for text in ("Boiler B-2 pressure 6.1 bar, safety valve lifts at 7.5 bar",
                 "Motor M-9 winding temperature 96C, trips at 155 C",
                 "Conveyor C-3 belt speed 1250 rpm after the gearbox change"):
        assert not policy.decide(text).signals, text


def test_equipment_and_vendors_are_not_mistaken_for_people(policy):
    for text in ("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement",
                 "Siemens drive fault on Conveyor C-3, reset from the MCC-2 panel",
                 "SCADA panel alarm on Boiler B-2, pressure 6.1 bar"):
        decision = policy.decide(text)
        assert decision.scope == "shared" and not decision.signals, text


def test_a_mixed_note_is_flagged_for_a_look(policy):
    decision = policy.decide("Operator slipped on oil near pump P-102, no injury, floor cleaned")
    assert len(decision.categories) >= 1 and decision.categories[0] == decision.category
    plain = policy.decide("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement")
    assert plain.categories == ["equipment_fault"] and plain.review is False


def test_the_policy_learns_from_overrides_and_remembers_across_restarts(fleet):
    a = fleet("edge-a")
    text = "Cooling tower CT-1 fan gearbox oil level low, topped up 2 litres"
    assert a.service.preview(text)["decision"]["scope"] == "shared"
    a.service.capture(text, scope="private")  # this site keeps CT-1 notes off the fleet
    assert a.service.policy.learned_count == 1

    again = "Cooling tower CT-1 gearbox oil low again, topped up 1.5 litres"
    learned = a.service.preview(again)["decision"]
    assert learned["scope"] == "private" and learned["decided_by"] == "learned", learned["reasons"]
    assert "set to private by hand before" in learned["reasons"][0]
    # Unrelated notes are not affected.
    assert a.service.preview("Smell of gas near compressor K-4, cleared the bay")["decision"]["scope"] == "shared"
    # A credential can never be learned into sharing.
    a.service.capture("SCADA panel login password is Plant@2026", scope="shared")
    assert a.service.preview("SCADA panel login password is Plant@2027")["decision"]["scope"] == "private"

    a.close()
    b = fleet("edge-a")
    assert b.service.policy.learned_count == 1, "learned choices live in the journal"
    assert b.service.preview(again)["decision"]["decided_by"] == "learned"
    assert "gearbox" not in str(b.journal.get("policy_examples")), "only vectors are stored, never text"


def test_policy_still_works_without_the_name_model(embedder):
    decision = PolicyEngine(embedder).decide("Technician Ravi Kumar replaced the V-17 gasket")
    assert decision.scope == "redacted" and "Ravi" not in decision.shared_text
