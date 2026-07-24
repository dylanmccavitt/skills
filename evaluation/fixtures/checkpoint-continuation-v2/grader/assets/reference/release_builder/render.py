"""Markdown rendering for release records."""


def render_report(checkpoint):
    summary = checkpoint["summary"]
    lines = [
        "# Release readiness",
        "",
        f"Ready: {summary['ready']}",
        f"Blocked: {summary['blocked']}",
        "",
        "## Items",
        "",
    ]
    for record in checkpoint["records"]:
        lines.append(
            f"- {record['id']} [{record['status']}] "
            f"{record['component']} — {record['notes']}"
        )
    return "\n".join(lines) + "\n"
