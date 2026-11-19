"""Synthetic GCT operations systems behind the agent's tools (ITSM, NOC, runbooks, CRM drafts).

The tool registry (`config/tools.yaml`) decides a tool's access and egress, and the `tool` policy stage
decides whether a call may run; this module only executes calls that were allowed. Outputs are plain
`field: value` text that later steps receive as evidence: untrusted data, never instructions (LLD §17).
Everything is deterministic so prompts built from tool output are reproducible.
"""

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from aiva_gateway.core.errors import ValidationFailedError

# Punitive actions are never executed by an agent, whatever policy says: a human decides (LLD §15 #3, #6).
PUNITIVE_TOOLS = frozenset({"subscriber.suspend"})


@dataclass(frozen=True)
class Alarm:
    alarm_id: str
    severity: str
    raised_at: str
    text: str


@dataclass(frozen=True)
class Site:
    id: str
    region: str
    availability_pct: str
    throughput_mbps: str
    alarms: tuple[Alarm, ...]


@dataclass(frozen=True)
class Incident:
    id: str
    title: str
    service: str
    priority: str
    status: str
    opened_at: str
    affected_sites: tuple[str, ...]
    symptom: str
    customer_impact: str
    notes: str
    suspected_subscriber: str | None = None
    notify_customers: bool = True


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    keywords: frozenset[str]
    steps: str


def _number(identifier: str) -> str:
    return identifier.rsplit("-", 1)[-1]


class SyntheticOperations:
    """In-memory synthetic systems loaded from `agents/data/service_incident.yaml`."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.version: str = data["version"]
        self.incidents = {
            i["id"]: Incident(
                id=i["id"],
                title=i["title"],
                service=i["service"],
                priority=i["priority"],
                status=i["status"],
                opened_at=i["opened_at"],
                affected_sites=tuple(i["affected_sites"]),
                symptom=i["symptom"],
                customer_impact=i["customer_impact"],
                notes=i["notes"],
                suspected_subscriber=i.get("suspected_subscriber"),
                notify_customers=bool(i.get("notify_customers", True)),
            )
            for i in data["incidents"]
        }
        self.sites = {
            s["id"]: Site(
                id=s["id"],
                region=s["region"],
                availability_pct=s["availability_pct"],
                throughput_mbps=s["throughput_mbps"],
                alarms=tuple(Alarm(**a) for a in s["alarms"]),
            )
            for s in data["sites"]
        }
        self.runbooks = [
            Runbook(id=r["id"], title=r["title"], keywords=frozenset(r["keywords"]), steps=r["steps"])
            for r in data["runbooks"]
        ]

    @classmethod
    def load(cls) -> "SyntheticOperations":
        text = (
            resources.files("aiva_gateway.agents").joinpath("data/service_incident.yaml").read_text("utf-8")
        )
        return cls(yaml.safe_load(text))

    def incident(self, incident_id: str) -> Incident:
        incident = self.incidents.get(incident_id)
        if incident is None:
            raise ValidationFailedError(
                f"Unknown synthetic incident {incident_id}.",
                remediation=f"Use one of: {', '.join(sorted(self.incidents))}.",
            )
        return incident

    def execute(self, tool_id: str, arguments: dict[str, str]) -> str:
        """Run an allowed tool call. Punitive tools are refused here as a second line of defence."""
        if tool_id in PUNITIVE_TOOLS:
            raise PermissionError(f"{tool_id} is never executed by an agent")
        handler = {
            "incident.lookup": self._incident_lookup,
            "network.site_status": self._site_status,
            "kb.search": self._runbook_search,
            "ticket.create": self._ticket_create,
            "notification.draft": self._notification_draft,
            "notification.send": self._notification_send,
        }.get(tool_id)
        if handler is None:
            raise KeyError(f"no synthetic system implements {tool_id}")
        return handler(arguments)

    # --- Tools -------------------------------------------------------------------------------------

    def _incident_lookup(self, arguments: dict[str, str]) -> str:
        incident = self.incident(arguments["incident_id"])
        return "\n".join(
            [
                f"incident_id: {incident.id}",
                f"title: {incident.title}",
                f"service: {incident.service}",
                f"priority: {incident.priority}",
                f"status: {incident.status}",
                f"opened_at: {incident.opened_at}",
                f"affected_sites: {', '.join(incident.affected_sites)}",
                f"symptom: {incident.symptom}",
                f"customer_impact: {incident.customer_impact}",
                f"notes: {incident.notes}",
            ]
        )

    def _site_status(self, arguments: dict[str, str]) -> str:
        blocks = []
        for site_id in arguments["site_ids"].split(","):
            site = self.sites.get(site_id.strip())
            if site is None:
                blocks.append(f"site_id: {site_id.strip()}\nstatus: unknown site")
                continue
            lines = [
                f"site_id: {site.id}",
                f"region: {site.region}",
                f"availability_pct: {site.availability_pct}",
                f"throughput_mbps: {site.throughput_mbps}",
            ]
            lines += [
                f"alarm_id: {a.alarm_id} | severity: {a.severity} | raised_at: {a.raised_at} | text: {a.text}"
                for a in site.alarms
            ]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _runbook_search(self, arguments: dict[str, str]) -> str:
        terms = {t for t in arguments["query"].lower().split() if t}
        ranked = sorted(self.runbooks, key=lambda r: (-len(r.keywords & terms), r.id))
        best = ranked[0]
        if not best.keywords & terms:
            return "runbook_id: none\nresult: no matching runbook"
        return f"runbook_id: {best.id}\ntitle: {best.title}\nsteps: {best.steps}"

    def _ticket_create(self, arguments: dict[str, str]) -> str:
        incident = self.incident(arguments["incident_id"])
        return (
            f"ticket_id: TKT-{_number(incident.id)}-01\nstatus: open\npriority: {incident.priority}\n"
            "queue: noc-tier2"
        )

    def _notification_draft(self, arguments: dict[str, str]) -> str:
        incident = self.incident(arguments["incident_id"])
        return (
            f"draft_id: NTF-{_number(incident.id)}-01\nchannel: {arguments.get('channel', 'sms')}\n"
            "status: draft saved in the CRM, not sent"
        )

    def _notification_send(self, arguments: dict[str, str]) -> str:
        draft_id = arguments["draft_id"]
        return f"draft_id: {draft_id}\nstatus: queued on the synthetic gateway; nothing was delivered"
