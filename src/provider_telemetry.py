from __future__ import annotations

import re


def first_number(value: object, *paths: str) -> float | int | None:
    for path in paths:
        current = value
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if isinstance(current, (float, int)) and not isinstance(current, bool):
                return current
    return None


def normalise_usage(kind: str, value: object) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    if kind == "runpod":
        credit = first_number(
            value,
            "credit",
            "balance",
            "data.myself.credit",
            "data.myself.balance",
        )
        if credit is not None:
            return [{"label": "Credit balance", "unit": "USD", "value": credit}]
        records = value if isinstance(value, list) else []
        amount = sum(
            float(record.get("amount", 0))
            for record in records
            if isinstance(record, dict)
        )
        return [{"label": "Spend", "unit": "USD", "value": round(amount, 4)}]
    if kind == "vast":
        credit = first_number(value, "credit", "balance")
        if credit is not None:
            metrics.append({"label": "Credit balance", "unit": "USD", "value": credit})
        for label, paths in (
            ("Current spend", ("current_spend", "currentSpend")),
            ("Total spent", ("total_spent", "totalSpent", "spent")),
        ):
            found = first_number(value, *paths)
            if found is not None:
                metrics.append({"label": label, "unit": "USD", "value": found})
        return metrics
    if kind == "salad":
        credit = first_number(
            value,
            "credit",
            "credit_balance",
            "credits",
            "current_credit",
        )
        if credit is not None:
            metrics.append({"label": "Credit balance", "unit": "USD", "value": credit})
        used = first_number(value, "container_groups_quotas.container_replicas_used")
        quota = first_number(value, "container_groups_quotas.container_replicas_quota")
        if used is not None:
            metrics.append({"label": "Replicas used", "value": used})
        if quota is not None:
            metrics.append({"label": "Replica quota", "value": quota})
        if used is not None and quota is not None:
            metrics.append({"label": "Replicas available", "value": quota - used})
        return metrics
    if kind == "cliproxyapi":
        for label, paths in (
            ("Requests", ("usage.total_requests", "total_requests")),
            ("Successful", ("usage.successful_requests", "successful_requests")),
            ("Failed", ("usage.failed_requests", "failed_requests")),
            ("Tokens", ("usage.total_tokens", "total_tokens")),
        ):
            found = first_number(value, *paths)
            if found is not None:
                metrics.append({"label": label, "value": found})
        return metrics
    raise ValueError(f"unsupported usage kind: {kind}")


def xai_user_id(account: dict[str, object]) -> str | None:
    records = [account]
    for key in ("attributes", "metadata", "oauth", "user"):
        value = account.get(key)
        if isinstance(value, dict):
            records.append(value)
    for record in records:
        for key in ("sub", "subject", "user_id", "userId", "id"):
            value = record.get(key)
            if isinstance(value, (int, str)) and str(value).strip():
                return str(value).strip()
    return None


def normalise_xai_quota(value: object, account: int) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    config = value.get("config")
    if not isinstance(config, dict):
        return []
    detail = f"Account {account}"
    metrics: list[dict[str, object]] = []
    usage_percent = first_number(config, "creditUsagePercent", "credit_usage_percent")
    if usage_percent is not None:
        metrics.append(
            {
                "detail": detail,
                "label": "Weekly remaining",
                "unit": "%",
                "value": max(0, round(100 - usage_percent, 2)),
            }
        )
    products = config.get("productUsage") or config.get("product_usage")
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            name = str(product.get("product") or "").strip()
            words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("-", " ")
            words = " ".join(words.split())
            if not any(
                word in words.casefold() for word in ("build", "chat", "imagine")
            ):
                continue
            used = first_number(product, "usagePercent", "usage_percent")
            if used is not None:
                metrics.append(
                    {
                        "detail": detail,
                        "label": f"{words} remaining",
                        "unit": "%",
                        "value": max(0, round(100 - used, 2)),
                    }
                )
    return metrics


def deduplicate_metrics(
    metrics: list[dict[str, object]],
) -> list[dict[str, object]]:
    return list(
        {
            (str(metric.get("label")), str(metric.get("detail", ""))): metric
            for metric in metrics
        }.values()
    )


def selected_fields(value: object, *names: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in names if name in value}
