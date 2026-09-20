"""Human review console for quarantined governed memories."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

import streamlit as st
from dotenv import load_dotenv

from servicemind.review_ui import MemoryReviewClient, MemoryReviewClientError, ReviewItem


def _api_url() -> str:
    load_dotenv()
    return os.getenv("SERVICEMIND_REVIEW_API_URL", "http://127.0.0.1:18080")


def _require_access_token() -> str:
    if not st.user.is_logged_in:
        st.info("Sign in with the enterprise identity provider to review governed memories.")
        if st.button("Sign in", type="primary"):
            st.login("keycloak")
        st.stop()
    access_token = st.user.tokens.to_dict().get("access")
    if not access_token:
        st.error("The identity provider did not return an API access token.")
        if st.button("Sign out"):
            st.logout()
        st.stop()
    return access_token


def _render_item(client: MemoryReviewClient, item: ReviewItem) -> None:
    title = f"{item.subject_key} · v{item.version} · {item.memory_id}"
    with st.expander(title, expanded=False):
        left, right = st.columns(2)
        left.metric("Confidence", f"{item.confidence:.3f}")
        right.metric("Importance", f"{item.importance:.3f}")
        st.caption(f"Created {item.created_at.isoformat()} · type={item.memory_type}")
        st.markdown("**Proposed memory**")
        st.code(item.content, language=None, wrap_lines=True)
        st.markdown("**Supporting episodes**")
        st.code("\n".join(str(value) for value in item.supporting_episode_ids) or "none")
        with st.popover("Evidence and provenance"):
            st.json(
                {
                    "evidence_refs": item.evidence_refs,
                    "provenance": item.provenance,
                    "content_hash": item.content_hash,
                }
            )

        with st.form(f"review-{item.memory_id}", clear_on_submit=False):
            decision = cast(
                "Literal['activate', 'reject']",
                st.radio(
                    "Decision",
                    options=("activate", "reject"),
                    horizontal=True,
                    format_func=lambda value: (
                        "Approve and activate" if value == "activate" else "Reject"
                    ),
                ),
            )
            review_ref = st.text_input(
                "Review reference",
                placeholder="CAB ticket, change record, or review URL",
            )
            comment = st.text_area("Decision rationale", max_chars=1000)
            confirmed = st.checkbox(
                "I reviewed the content, evidence, scope, and supporting episodes shown above."
            )
            submitted = st.form_submit_button(
                "Submit atomic decision",
                type="primary" if decision == "activate" else "secondary",
                disabled=not confirmed,
            )
        if submitted:
            if len(review_ref.strip()) < 3 or len(comment.strip()) < 3:
                st.error("Review reference and rationale must each contain at least 3 characters.")
                return
            try:
                updated = client.decide(
                    item,
                    decision=decision,
                    review_ref=review_ref.strip(),
                    comment=comment.strip(),
                )
            except MemoryReviewClientError as exc:
                if exc.status_code == 401:
                    st.error("Your access token expired. Sign out and sign in again.")
                elif exc.status_code == 409:
                    st.error("This item changed after loading. Refresh the queue before deciding.")
                else:
                    st.error(str(exc))
                return
            st.session_state.review_notice = (
                f"Decision recorded: {updated.subject_key} → {updated.status}."
            )
            st.rerun()


st.set_page_config(page_title="Memory Review · ServiceMind", page_icon="✅", layout="wide")
st.title("Governed memory review")
st.caption(
    "Pending procedures remain quarantined until an approver binds a decision to the exact "
    "version and content hash shown here."
)

token = _require_access_token()
client = MemoryReviewClient(base_url=_api_url(), access_token=token)

if notice := st.session_state.pop("review_notice", None):
    st.success(notice)

with st.sidebar:
    st.write(f"Signed in as **{st.user.get('preferred_username', st.user.get('name', 'user'))}**")
    if st.button("Sign out", use_container_width=True):
        st.logout()

    def _reset_review_pagination() -> None:
        st.session_state.review_cursors = []

    memory_type = st.selectbox(
        "Memory type",
        ("procedural", "episodic", "semantic"),
        key="review_memory_type",
        on_change=_reset_review_pagination,
    )
    limit = st.select_slider(
        "Items per page",
        options=(10, 25, 50, 100),
        value=25,
        key="review_page_size",
        on_change=_reset_review_pagination,
    )
    if st.button("Refresh", use_container_width=True):
        st.session_state.review_cursors = []
        st.rerun()

cursors: list[tuple[datetime, UUID]] = st.session_state.setdefault("review_cursors", [])
cursor = cursors[-1] if cursors else None
try:
    page = client.list_queue(
        memory_type=memory_type,
        limit=limit,
        after_created_at=None if cursor is None else cursor[0],
        after_memory_id=None if cursor is None else cursor[1],
    )
except MemoryReviewClientError as exc:
    if exc.status_code == 401:
        st.error("Your access token expired. Sign out and sign in again.")
    elif exc.status_code == 403:
        st.error("Your account does not have the approver role for this queue.")
    else:
        st.error(str(exc))
    st.stop()

if not page.items:
    st.info("No reviewable memories are visible in this scope.")
for review_item in page.items:
    _render_item(client, review_item)

previous, next_page = st.columns(2)
if previous.button("Previous page", disabled=not cursors, use_container_width=True):
    cursors.pop()
    st.rerun()
if next_page.button(
    "Next page",
    disabled=page.next_after_created_at is None or page.next_after_memory_id is None,
    use_container_width=True,
):
    assert page.next_after_created_at is not None and page.next_after_memory_id is not None
    cursors.append((page.next_after_created_at, page.next_after_memory_id))
    st.rerun()
