from __future__ import annotations


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
            "clientBalance",
            "credit",
            "balance",
            "data.myself.clientBalance",
            "data.myself.credit",
            "data.myself.balance",
        )
        if credit is not None:
            return [{"label": "Credit", "unit": "USD", "value": credit}]
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
            metrics.append({"label": "Credit", "unit": "USD", "value": credit})
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
            metrics.append({"label": "Credit", "unit": "USD", "value": credit})
        spend = first_number(
            value,
            "current_spend",
            "currentSpend",
            "month_spend",
            "monthSpend",
            "spend",
        )
        if spend is not None:
            metrics.append({"label": "Spend", "unit": "USD", "value": spend})
        used = first_number(value, "container_groups_quotas.container_replicas_used")
        quota = first_number(value, "container_groups_quotas.container_replicas_quota")
        if used is not None and quota is not None and quota > 0:
            available = max(0, quota - used)
            metrics.append(
                {
                    "detail": f"{available:g} Of {quota:g} Available",
                    "label": "Replica capacity remaining",
                    "unit": "%",
                    "value": round(available / quota * 100, 2),
                }
            )
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
            name = str(product.get("product") or "").casefold()
            product_name = next(
                (
                    canonical
                    for key, canonical in (
                        ("build", "Grok Build"),
                        ("chat", "Grok Chat"),
                        ("imagine", "Grok Imagine"),
                    )
                    if key in name
                ),
                None,
            )
            if product_name is None:
                continue
            used = first_number(product, "usagePercent", "usage_percent")
            if used is not None:
                metrics.append(
                    {
                        "detail": detail,
                        "label": f"{product_name} remaining",
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
