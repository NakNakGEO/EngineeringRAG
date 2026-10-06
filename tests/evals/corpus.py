"""A controlled fixture: a small payments/orders codebase with designed dependency chains, a
knowledge base that includes stale and near-duplicate statements, and labelled questions.

No company data. Everything here is synthetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import utcnow
from eios_domain.knowledge import Health, KnowledgeItemCreate, SourceKind, Trust
from eios_domain.vault import Vault
from eios_retrieval.evaluation import EvalCase
from eios_runtime import Container
from eios_storage.tables.knowledge import item as item_t
from tests.gitfixtures import make_repo

SHOP: dict[str, str] = {
    "shop/__init__.py": "",
    "shop/common/__init__.py": "",
    "shop/common/clock.py": "import time\n\n\ndef now_ms():\n    return int(time.time() * 1000)\n",
    "shop/common/errors.py": (
        "class PaymentError(Exception):\n    pass\n\n\n"
        "def is_transient(error):\n    return isinstance(error, TimeoutError)\n"
    ),
    "shop/payments/__init__.py": "",
    "shop/payments/retry.py": (
        "from shop.common.clock import now_ms\nfrom shop.common.errors import is_transient\n\n"
        "MAX_ATTEMPTS = 5\n\n\nclass RetryPolicy:\n"
        "    def __init__(self, base_delay_ms=200):\n        self.base_delay_ms = base_delay_ms\n\n"
        "    def compute_backoff(self, attempt):\n        started = now_ms()\n"
        "        return (started % 7) + self.base_delay_ms * (2 ** attempt)\n\n"
        "    def should_retry(self, error, attempt):\n"
        "        return attempt < MAX_ATTEMPTS and is_transient(error)\n"
    ),
    "shop/payments/ledger.py": (
        "class LedgerEntry:\n    def __init__(self, order_id, amount):\n"
        "        self.order_id = order_id\n        self.amount = amount\n\n\n"
        "def record_charge(order_id, amount):\n    return LedgerEntry(order_id, amount)\n"
    ),
    "shop/payments/gateway.py": (
        "from shop.payments.retry import RetryPolicy\n"
        "from shop.payments.ledger import record_charge\n"
        "from shop.common.errors import PaymentError\n\n\n"
        "class PaymentGateway:\n    def __init__(self):\n        self.policy = RetryPolicy()\n\n"
        "    def charge(self, order_id, amount):\n        attempt = 0\n        while True:\n"
        "            try:\n                return self._submit(order_id, amount)\n"
        "            except TimeoutError as error:\n"
        "                if not self.policy.should_retry(error, attempt):\n"
        "                    raise PaymentError('charge failed')\n                attempt += 1\n\n"
        "    def _submit(self, order_id, amount):\n        return record_charge(order_id, amount)\n"
    ),
    "shop/inventory/__init__.py": "",
    "shop/inventory/stock.py": (
        "class StockLevel:\n    def available(self, sku):\n        return True\n"
    ),
    "shop/inventory/reserve.py": (
        "from shop.inventory.stock import StockLevel\n\n\n"
        "def reserve_items(items):\n    level = StockLevel()\n"
        "    return [i for i in items if level.available(i)]\n\n\n"
        "def release_items(items):\n    return list(items)\n"
    ),
    "shop/notifications/__init__.py": "",
    "shop/notifications/templates.py": (
        "def render_receipt(order_id, total):\n    return f'Receipt {order_id}: {total}'\n"
    ),
    "shop/notifications/email.py": (
        "from shop.notifications.templates import render_receipt\n\n\n"
        "def send_receipt(address, order_id, total):\n"
        "    body = render_receipt(order_id, total)\n    return address, body\n"
    ),
    "shop/orders/__init__.py": "",
    "shop/orders/service.py": (
        "from shop.payments.gateway import PaymentGateway\n"
        "from shop.inventory.reserve import reserve_items\n\n\n"
        "class OrderService:\n    def place_order(self, items, order_id, amount):\n"
        "        reserved = reserve_items(items)\n"
        "        PaymentGateway().charge(order_id, amount)\n        return reserved\n"
    ),
    "shop/auth/__init__.py": "",
    "shop/auth/tokens.py": (
        "def issue_token(user):\n    return f'token-{user}'\n\n\n"
        "def verify_token(token):\n    return token.startswith('token-')\n"
    ),
    "shop/api/__init__.py": "",
    "shop/api/routes.py": (
        "from shop.orders.service import OrderService\n"
        "from shop.notifications.email import send_receipt\n"
        "from shop.auth.tokens import verify_token\n\n\n"
        "def handle_checkout(token, items, address):\n    verify_token(token)\n"
        "    reserved = OrderService().place_order(items, 'o-1', 10)\n"
        "    send_receipt(address, 'o-1', 10)\n    return reserved\n"
    ),
    "tests/test_retry.py": (
        "from shop.payments.retry import RetryPolicy\n\n\n"
        "def test_backoff_grows():\n    p = RetryPolicy()\n"
        "    assert p.compute_backoff(2) > p.compute_backoff(1) - 7\n"
    ),
    "tests/test_orders.py": (
        "from shop.orders.service import OrderService\n\n\n"
        "def test_place_order():\n    assert OrderService().place_order(['a'], 'o', 1) == ['a']\n"
    ),
    "db/schema.sql": (
        "CREATE TABLE orders (id INT PRIMARY KEY, total INT);\n"
        "CREATE TABLE ledger_entries (id INT PRIMARY KEY, order_id INT REFERENCES orders(id));\n"
        "CREATE PROCEDURE usp_record_charge @order INT AS\nBEGIN\n"
        "    INSERT INTO ledger_entries (order_id) VALUES (@order);\nEND\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n\nThe shop is split into payments, orders, inventory and notifications.\n"
    ),
}

CHUNK_KIND = "code_chunk"


@dataclass(frozen=True)
class KnowledgeSpec:
    logical: str
    title: str
    content: str
    kind: str = "rule"
    trust: Trust = Trust.VERIFIED
    health: Health = Health.CURRENT
    subject: str | None = None
    path: str | None = None
    age_days: int = 0


KNOWLEDGE: list[KnowledgeSpec] = [
    KnowledgeSpec(
        "kn:retry",
        "Payment retry policy",
        "Failed card charges are retried up to five times with exponential backoff starting at "
        "200 ms. Only transient errors such as timeouts are retried; declined cards are not.",
        subject="payment-retry",
        path="shop/payments/retry.py",
    ),
    KnowledgeSpec(
        "kn:retry-copy",
        "Payment retry policy (copy)",
        "Failed card charges are retried up to five times with exponential backoff starting at "
        "200 ms. Only transient errors such as timeouts are retried; declined cards are not.",
        trust=Trust.OBSERVED,
        health=Health.UNVERIFIED,
        subject="payment-retry",
    ),
    KnowledgeSpec(
        "kn:retry-stale",
        "Old payment retry behaviour",
        "Failed card charges used to be retried every 30 seconds with a fixed delay and no "
        "backoff. Retry delay was constant.",
        trust=Trust.DERIVED,
        health=Health.STALE,
        subject="payment-retry",
        age_days=500,
    ),
    KnowledgeSpec(
        "kn:ledger",
        "Ledger invariant",
        "Every successful card charge must create exactly one ledger entry before the order is "
        "confirmed. A charge without a ledger entry is a reconciliation incident.",
        subject="ledger-invariant",
        path="shop/payments/ledger.py",
    ),
    KnowledgeSpec(
        "kn:receipt",
        "Receipt email privacy",
        "Receipt emails must never include card numbers, CVV codes or full billing addresses; "
        "only the last four digits of the card may appear.",
        subject="receipt-privacy",
        path="shop/notifications/email.py",
    ),
    KnowledgeSpec(
        "kn:card-storage",
        "Card storage policy",
        "Card numbers are never stored by the platform. Only tokenised references issued by the "
        "payment provider are kept in the database.",
        trust=Trust.APPROVED,
        subject="card-storage",
    ),
    KnowledgeSpec(
        "kn:card-storage-copy",
        "Card storage policy (restated)",
        "Card numbers are never stored by the platform. Only tokenised references issued by the "
        "payment provider are kept in the database.",
        trust=Trust.OBSERVED,
        health=Health.UNVERIFIED,
        subject="card-storage",
    ),
    KnowledgeSpec(
        "kn:inventory",
        "Inventory release on failure",
        "Inventory reservations are released automatically when payment fails so stock is not "
        "held by abandoned orders.",
        kind="note",
        trust=Trust.DERIVED,
        path="shop/inventory/reserve.py",
    ),
    KnowledgeSpec(
        "kn:noise-k8s",
        "Ingress certificates",
        "Kubernetes ingress certificates rotate monthly.",
        "note",
    ),
    KnowledgeSpec(
        "kn:noise-naming", "Naming", "Modules use snake case names; classes use CamelCase.", "note"
    ),
    KnowledgeSpec(
        "kn:noise-okr",
        "Quarterly planning",
        "Quarterly objectives are reviewed in the planning meeting.",
        "note",
    ),
]


def _sym(path: str, qualified: str) -> str:
    return f"symbol:{path}::{qualified}"


CASES: list[EvalCase] = [
    EvalCase(
        "backoff-deps",
        "What does `RetryPolicy.compute_backoff` do and what does it depend on?",
        relevant=frozenset(
            {
                _sym("shop/payments/retry.py", "RetryPolicy.compute_backoff"),
                _sym("shop/common/clock.py", "now_ms"),
            }
        ),
        required_deps=frozenset({_sym("shop/common/clock.py", "now_ms")}),
        top1=frozenset({_sym("shop/payments/retry.py", "RetryPolicy.compute_backoff")}),
    ),
    EvalCase(
        "charge-deps",
        "Before I change `PaymentGateway.charge`, what does it rely on?",
        relevant=frozenset(
            {
                _sym("shop/payments/gateway.py", "PaymentGateway.charge"),
                _sym("shop/payments/retry.py", "RetryPolicy.should_retry"),
            }
        ),
        required_deps=frozenset(
            {
                _sym("shop/payments/retry.py", "RetryPolicy.should_retry"),
                _sym("shop/payments/gateway.py", "PaymentGateway._submit"),
                _sym("shop/common/errors.py", "PaymentError"),
            }
        ),
        support=frozenset({"kn:ledger"}),
        top1=frozenset({_sym("shop/payments/gateway.py", "PaymentGateway.charge")}),
    ),
    EvalCase(
        "order-flow",
        "How does `OrderService.place_order` reserve inventory and take payment?",
        relevant=frozenset(
            {
                _sym("shop/orders/service.py", "OrderService.place_order"),
                _sym("shop/inventory/reserve.py", "reserve_items"),
                _sym("shop/payments/gateway.py", "PaymentGateway.charge"),
            }
        ),
        required_deps=frozenset(
            {
                _sym("shop/inventory/reserve.py", "reserve_items"),
                _sym("shop/payments/gateway.py", "PaymentGateway.charge"),
            }
        ),
        top1=frozenset({_sym("shop/orders/service.py", "OrderService.place_order")}),
    ),
    EvalCase(
        "checkout-calls",
        "What does `handle_checkout` call?",
        relevant=frozenset(
            {
                _sym("shop/api/routes.py", "handle_checkout"),
                _sym("shop/orders/service.py", "OrderService.place_order"),
                _sym("shop/notifications/email.py", "send_receipt"),
                _sym("shop/auth/tokens.py", "verify_token"),
            }
        ),
        required_deps=frozenset(
            {
                _sym("shop/orders/service.py", "OrderService.place_order"),
                _sym("shop/notifications/email.py", "send_receipt"),
                _sym("shop/auth/tokens.py", "verify_token"),
            }
        ),
        top1=frozenset({_sym("shop/api/routes.py", "handle_checkout")}),
    ),
    EvalCase(
        "retry-prose",
        "How are failed card payments retried and with what backoff?",
        relevant=frozenset(
            {
                "kn:retry",
                _sym("shop/payments/retry.py", "RetryPolicy.compute_backoff"),
                _sym("shop/payments/retry.py", "RetryPolicy.should_retry"),
            }
        ),
        support=frozenset({"kn:retry"}),
        top1=frozenset({"kn:retry"}),
    ),
    EvalCase(
        "retry-delay",
        "How long is the retry delay between charge attempts?",
        relevant=frozenset(
            {"kn:retry", _sym("shop/payments/retry.py", "RetryPolicy.compute_backoff")}
        ),
        support=frozenset({"kn:retry"}),
        top1=frozenset({"kn:retry", _sym("shop/payments/retry.py", "RetryPolicy.compute_backoff")}),
    ),
    EvalCase(
        "receipt",
        "Where is the receipt email built and what must it never contain?",
        relevant=frozenset(
            {
                _sym("shop/notifications/email.py", "send_receipt"),
                _sym("shop/notifications/templates.py", "render_receipt"),
                "kn:receipt",
            }
        ),
        required_deps=frozenset({_sym("shop/notifications/templates.py", "render_receipt")}),
        support=frozenset({"kn:receipt"}),
    ),
    EvalCase(
        "card-storage",
        "What is the policy on storing card numbers?",
        relevant=frozenset({"kn:card-storage"}),
        support=frozenset({"kn:card-storage"}),
        top1=frozenset({"kn:card-storage"}),
    ),
    EvalCase(
        "retry-tests",
        "What tests cover the retry policy?",
        relevant=frozenset(
            {"file:tests/test_retry.py", _sym("shop/payments/retry.py", "RetryPolicy")}
        ),
        support=frozenset({"file:tests/test_retry.py"}),
    ),
    EvalCase(
        "ledger-sql",
        "What does the schema say about ledger entries?",
        relevant=frozenset(
            {_sym("db/schema.sql", "ledger_entries"), _sym("db/schema.sql", "usp_record_charge")}
        ),
        top1=frozenset({_sym("db/schema.sql", "ledger_entries")}),
    ),
    EvalCase(
        "unknown-entity",
        "Explain the behaviour of `QuantumLedgerSynchronizer` during checkout",
        relevant=frozenset(),
        expect_ready=False,
    ),
]


@dataclass
class Corpus:
    project_id: uuid.UUID
    branch: str
    cases: list[EvalCase]
    knowledge_ids: dict[str, str]


async def build_corpus(container: Container, db: AsyncEngine, workspace: Path) -> Corpus:
    """Create the repository, index it, add knowledge + baseline code chunks."""
    repo = make_repo(workspace, "shop", dict(SHOP), remote="https://example.invalid/acme/shop.git")
    boot = await container.projects.bootstrap(repo)
    assert boot.project is not None
    await container.projects.sync_now(boot.project.id)
    project = await container.projects.require(boot.project.id)
    pid = project.id
    branch = project.last_branch or "main"

    ids: dict[str, str] = {}
    for spec in KNOWLEDGE:
        item = await container.knowledge.add_item(
            KnowledgeItemCreate(
                vault=Vault.PROJECT,
                project_id=pid,
                kind=spec.kind,
                title=spec.title,
                content=spec.content,
                trust=spec.trust,
                health=Health.UNVERIFIED if spec.health is Health.CURRENT else spec.health,
                source_kind=SourceKind.MANUAL,
                created_by="eval-fixture",
                subject_key=spec.subject,
                metadata={"logical_id": spec.logical, **({"path": spec.path} if spec.path else {})},
            )
        )
        ids[spec.logical] = str(item.id)
        # health CURRENT is not creatable for RAW trust, so set it directly after creation
        async with db.begin() as conn:
            values: dict[str, object] = {"health": spec.health.value}
            if spec.age_days:
                values["updated_at"] = utcnow() - timedelta(days=spec.age_days)
            await conn.execute(sa.update(item_t).where(item_t.c.id == item.id).values(**values))

    # baseline corpus: every symbol and every file as a "chunk" (classic chunk-RAG)
    store = container.projects.store
    for file_row in await store.list_files(pid, branch, limit=1000):
        logical = f"file:{file_row.path}"
        src = await container.projects.read_source(pid, file_row.path, max_lines=400)
        if src is None:
            continue
        await container.knowledge.add_item(
            KnowledgeItemCreate(
                vault=Vault.PROJECT,
                project_id=pid,
                kind=CHUNK_KIND,
                title=file_row.path,
                content=src["text"][:4000] or file_row.path,
                trust=Trust.DERIVED,
                source_kind=SourceKind.CODE,
                created_by="chunker",
                provenance=[{"source": file_row.path, "actor": "chunker"}],  # type: ignore[list-item]
                metadata={"logical_id": logical, "path": file_row.path},
            )
        )
        for sym in await store.file_symbols(file_row.id):
            if sym.kind in {"variable"}:
                continue
            lines = src["text"].split("\n")[sym.start_line - 1 : sym.end_line]
            await container.knowledge.add_item(
                KnowledgeItemCreate(
                    vault=Vault.PROJECT,
                    project_id=pid,
                    kind=CHUNK_KIND,
                    title=sym.qualified_name,
                    content="\n".join(lines)[:4000] or sym.qualified_name,
                    trust=Trust.DERIVED,
                    source_kind=SourceKind.CODE,
                    created_by="chunker",
                    provenance=[{"source": sym.path, "actor": "chunker"}],  # type: ignore[list-item]
                    metadata={
                        "logical_id": f"symbol:{sym.path}::{sym.qualified_name}",
                        "path": sym.path,
                    },
                )
            )
    return Corpus(pid, branch, CASES, ids)
