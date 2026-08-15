from control.config import Target
from providers.economics import rank_targets


def test_modal_credit_ranks_before_hourly_capacity():
    targets = [
        Target(model="model", provider="vast-pod"),
        Target(model="model", provider="modal"),
    ]

    ranked = rank_targets(
        targets,
        {"modal": None, "vast-pod": 0.40},
        {"modal": {"metrics": [{"label": "Metered", "unit": "USD", "value": 12}]}},
    )

    assert [target.provider for target in ranked] == ["modal", "vast-pod"]


def test_known_cheapest_capacity_wins_after_modal_credit():
    targets = [
        Target(model="model", provider="modal"),
        Target(model="model", provider="runpod-pod"),
        Target(model="model", provider="vast-pod"),
    ]

    ranked = rank_targets(
        targets,
        {"modal": None, "runpod-pod": 0.44, "vast-pod": 0.40},
        {"modal": {"metrics": [{"label": "Metered", "unit": "USD", "value": 30}]}},
    )

    assert [target.provider for target in ranked] == [
        "vast-pod",
        "runpod-pod",
        "modal",
    ]
