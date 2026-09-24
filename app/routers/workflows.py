"""FastAPI Router for Jira AI Workflows (WF1 - WF8, Test Cases, PR Gate)."""

from __future__ import annotations

import logging
from typing import Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import reload_settings, settings
from app.dev_pr_gate import pr_context, pr_gate
from app.doc_review import DocReviewer
from app.exceptions import PromptNotFoundError
from app.llm_client import build_llm_client
from app.prompt_store import PromptStore
from app.schemas import (
    AlertBatchResponse,
    DevPrContextRequest,
    DevPrContextResponse,
    DevPrGateRequest,
    DevPrGateResponse,
    DocReviewRequest,
    DocReviewResponse,
    SlackMessageRequest,
    SlackMessageResponse,
    TestCaseDocRequest,
    TestCaseDocResponse,
    TransitionRecordRequest,
    TransitionRecordResponse,
    Workflow1ReviewRequest,
    Workflow1ReviewResponse,
    Workflow2ReplyRequest,
    Workflow2ReplyResponse,
    Workflow2Request,
    Workflow2Response,
    Workflow3SLAResponse,
    Workflow4DueDateResponse,
)
from app.slack_client import SlackClient
from app.slack_review_workflow import SlackReviewWorkflow
from app.testcase_chat_workflow import TestCaseChatWorkflow
from app.testcase_document import build_and_attach
from app.utilization import record_status_change
from app.workflow1_reviewer import Workflow1Reviewer
from app.workflow2_replier import Workflow2Replier
from app.workflow3_sla import Workflow3SLAChecker
from app.workflow4_aigov import (
    Workflow4AigovAssigneeChecker,
    Workflow4AigovTLChecker,
    Workflow4SummaryAssigneeChecker,
    Workflow4SummaryTLChecker,
)
from app.workflow4_due_date import Workflow4DueDateChecker
from app.workflow_governor_notify import GovernorNotifier

log = logging.getLogger(__name__)

router = APIRouter(tags=["Workflows"])
prompt_store = PromptStore(settings.prompt_dir)

SLACK_CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Send the reply of the user message and only "
    "in a single line of 11-21 words."
)


# ─── Workflow 1: Ticket Validation ───────────────────────────────────────────

@router.post("/workflow1", response_model=Workflow1ReviewResponse)
def workflow1_review(request: Workflow1ReviewRequest) -> Workflow1ReviewResponse:
    log.info("POST /workflow1 issue=%s", request.issueKey)
    try:
        reviewer = Workflow1Reviewer(settings=settings, prompt_store=prompt_store)
        return Workflow1ReviewResponse(**reviewer.review(request))
    except ValueError as exc:
        log.exception("/workflow1 validation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PromptNotFoundError, RuntimeError) as exc:
        log.exception("/workflow1 runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow1 failed")
        raise HTTPException(status_code=500, detail=f"workflow1 failed: {exc}") from exc


# ─── Workflow 2: Slack Q&A & Edit ───────────────────────────────────────────

@router.post("/workflow2/reply", response_model=Workflow2ReplyResponse)
def workflow2_reply(request: Workflow2ReplyRequest) -> Workflow2ReplyResponse:
    log.info(
        "POST /workflow2/reply user=%s channel=%s thread=%s",
        request.user_id,
        request.slack_channel_id,
        request.slack_thread_ts,
    )
    try:
        replier = Workflow2Replier(settings=settings, prompt_store=prompt_store)
        return Workflow2ReplyResponse(**replier.reply(request))
    except ValueError as exc:
        log.exception("/workflow2/reply validation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        log.exception("/workflow2/reply ticket lookup failed")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow2/reply failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/workflow2", response_model=Workflow2Response)
def workflow2_chat(request: Workflow2Request) -> Workflow2Response:
    log.info(
        "POST /workflow2 ticket=%s thread=%s msg_len=%d",
        request.ticket_key,
        request.slack_thread_ts,
        len(request.user_message),
    )
    try:
        workflow = TestCaseChatWorkflow(settings=settings)
        return Workflow2Response(**workflow.handle_message(request))
    except ValueError as exc:
        log.exception("/workflow2 validation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow2 failed")
        raise HTTPException(status_code=500, detail=f"workflow2 failed: {exc}") from exc


# ─── Workflow 3: SLA Monitoring ─────────────────────────────────────────────

@router.post("/workflow3", response_model=Workflow3SLAResponse)
@router.post("/workflow3/sla-check", response_model=Workflow3SLAResponse)
def workflow3_sla_check() -> Workflow3SLAResponse:
    log.info("POST /workflow3")
    try:
        checker = Workflow3SLAChecker(settings=settings)
        return Workflow3SLAResponse(**checker.check())
    except RuntimeError as exc:
        log.exception("/workflow3 runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow3 failed")
        raise HTTPException(status_code=500, detail=f"workflow3 failed: {exc}") from exc


# ─── Workflow 4: Due Date & Compliance ───────────────────────────────────────

@router.post("/workflow4", response_model=Workflow4DueDateResponse)
@router.post("/workflow4/due-date-check", response_model=Workflow4DueDateResponse)
def workflow4_due_date_check() -> Workflow4DueDateResponse:
    log.info("POST /workflow4")
    try:
        checker = Workflow4DueDateChecker(settings=settings)
        return Workflow4DueDateResponse(**checker.check())
    except RuntimeError as exc:
        log.exception("/workflow4 runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow4 failed")
        raise HTTPException(status_code=500, detail=f"workflow4 failed: {exc}") from exc


@router.post("/workflow4/daily-assignee", response_model=AlertBatchResponse)
def workflow4_daily_assignee() -> AlertBatchResponse:
    log.info("POST /workflow4/daily-assignee")
    try:
        return AlertBatchResponse(**Workflow4SummaryAssigneeChecker(settings=settings).check())
    except RuntimeError as exc:
        log.exception("/workflow4/daily-assignee runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow4/daily-assignee failed")
        raise HTTPException(status_code=500, detail=f"daily-assignee failed: {exc}") from exc


@router.post("/workflow4/tl", response_model=AlertBatchResponse)
def workflow4_tl() -> AlertBatchResponse:
    log.info("POST /workflow4/tl")
    try:
        return AlertBatchResponse(**Workflow4SummaryTLChecker(settings=settings).check())
    except RuntimeError as exc:
        log.exception("/workflow4/tl runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow4/tl failed")
        raise HTTPException(status_code=500, detail=f"workflow4/tl failed: {exc}") from exc


@router.post("/workflow4/aigov/daily-assignee", response_model=AlertBatchResponse)
def workflow4_aigov_daily_assignee() -> AlertBatchResponse:
    log.info("POST /workflow4/aigov/daily-assignee")
    try:
        return AlertBatchResponse(**Workflow4AigovAssigneeChecker(settings=settings).check())
    except RuntimeError as exc:
        log.exception("/workflow4/aigov/daily-assignee runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow4/aigov/daily-assignee failed")
        raise HTTPException(status_code=500, detail=f"aigov daily-assignee failed: {exc}") from exc


@router.post("/workflow4/aigov/tl", response_model=AlertBatchResponse)
def workflow4_aigov_tl() -> AlertBatchResponse:
    log.info("POST /workflow4/aigov/tl")
    try:
        return AlertBatchResponse(**Workflow4AigovTLChecker(settings=settings).check())
    except RuntimeError as exc:
        log.exception("/workflow4/aigov/tl runtime failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/workflow4/aigov/tl failed")
        raise HTTPException(status_code=500, detail=f"aigov workflow4/tl failed: {exc}") from exc


# ─── Governor Notify & Doc Review ────────────────────────────────────────────

@router.post("/workflow/governor-notify", response_model=AlertBatchResponse)
def workflow_governor_notify() -> AlertBatchResponse:
    log.info("POST /workflow/governor-notify")
    try:
        return AlertBatchResponse(**GovernorNotifier(settings=settings).notify())
    except Exception as exc:
        log.exception("/workflow/governor-notify failed")
        raise HTTPException(status_code=500, detail=f"governor-notify failed: {exc}") from exc


@router.post("/workflow/doc-review", response_model=DocReviewResponse)
@router.post("/doc-review", response_model=DocReviewResponse)
def workflow_doc_review(request: DocReviewRequest) -> DocReviewResponse:
    log.info("POST /workflow/doc-review issue=%s", request.issueKey)
    try:
        reviewer = DocReviewer(settings=settings, llm_client=build_llm_client(settings))
        return DocReviewResponse(**reviewer.review(request.issueKey, request.description))
    except Exception as exc:
        log.exception("/workflow/doc-review failed")
        raise HTTPException(status_code=500, detail=f"doc-review failed: {exc}") from exc


# ─── Status Transition Logger (WF8) ──────────────────────────────────────────

@router.post("/workflow/record-transition", response_model=TransitionRecordResponse)
def workflow_record_transition(request: TransitionRecordRequest) -> TransitionRecordResponse:
    log.info("POST /workflow/record-transition issue=%s %s->%s", request.issueKey, request.fromStatus, request.toStatus)
    try:
        result = record_status_change(
            settings,
            issue_key=request.issueKey,
            to_status=request.toStatus,
            from_status=request.fromStatus,
            project_key=request.projectKey,
            issue_type=request.issueType,
            assignee=request.assignee,
            changed_at=request.changedAt,
        )
        return TransitionRecordResponse(**result)
    except Exception as exc:
        log.exception("/workflow/record-transition failed")
        raise HTTPException(status_code=500, detail=f"record-transition failed: {exc}") from exc


# ─── Test Cases & PR Gate (WF5 & WF5b) ───────────────────────────────────────

@router.post("/testcases/dev/pr-gate", response_model=DevPrGateResponse)
def testcases_dev_pr_gate(request: DevPrGateRequest) -> DevPrGateResponse:
    log.info("POST /testcases/dev/pr-gate issue=%s", request.issueKey)
    try:
        return DevPrGateResponse(**pr_gate(request.issueKey, settings))
    except Exception as exc:
        log.exception("/testcases/dev/pr-gate failed")
        raise HTTPException(status_code=500, detail=f"pr-gate failed: {exc}") from exc


@router.post("/testcases/dev/pr-context", response_model=DevPrContextResponse)
def testcases_dev_pr_context(request: DevPrContextRequest) -> DevPrContextResponse:
    log.info("POST /testcases/dev/pr-context url=%s", request.prUrl)
    try:
        ctx = pr_context(request.prUrl, settings)
        return DevPrContextResponse(
            repo=ctx["repo"],
            owner=ctx["owner"],
            number=ctx["number"],
            prUrl=ctx["pr_url"],
            title=ctx.get("title", ""),
            headRef=ctx.get("head_ref", ""),
            headSha=ctx.get("head_sha", ""),
            baseRef=ctx.get("base_ref", ""),
            changedFiles=ctx.get("changed_files", []),
            contextText=ctx.get("context_text", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("/testcases/dev/pr-context failed")
        raise HTTPException(status_code=500, detail=f"pr-context failed: {exc}") from exc


@router.post("/testcases/document", response_model=TestCaseDocResponse)
def testcases_document(request: TestCaseDocRequest) -> TestCaseDocResponse:
    log.info(
        "POST /testcases/document issue=%s tcs=%d phase=%s",
        request.issueKey, len(request.testcases), request.phase,
    )
    try:
        return TestCaseDocResponse(
            **build_and_attach(request.issueKey, request.summary, request.testcases, phase=request.phase)
        )
    except Exception as exc:
        log.exception("/testcases/document failed")
        raise HTTPException(status_code=500, detail=f"document generation failed: {exc}") from exc


@router.post("/chat/slack-message", response_model=SlackMessageResponse)
def slack_chat_message(request: SlackMessageRequest) -> SlackMessageResponse:
    log.info(
        "POST /chat/slack-message user=%s channel=%s text=%r",
        request.user_id,
        request.slack_channel_id,
        request.user_message[:100],
    )
    try:
        llm_client = build_llm_client(settings)
        bot_reply = llm_client.complete(
            system_prompt=SLACK_CHAT_SYSTEM_PROMPT,
            user_message=request.user_message,
            max_tokens=60,
        )
        bot_reply = bot_reply.strip()

        slack_client = SlackClient(settings)
        slack_client.post_message(
            channel_id=request.slack_channel_id,
            text=bot_reply,
            thread_ts=request.slack_thread_ts,
        )

        return SlackMessageResponse(
            slack_channel_id=request.slack_channel_id,
            slack_thread_ts=request.slack_thread_ts,
            reply=bot_reply,
            status="sent",
        )
    except Exception as exc:
        log.exception("/chat/slack-message failed")
        raise HTTPException(status_code=500, detail=f"Failed to process Slack message: {exc}") from exc


# ─── Slack Events API Webhook (Direct Bot Integration) ────────────────────────

def _process_slack_event_async(event: dict[str, Any], team_id: str | None = None) -> None:
    """Asynchronous background worker for handling incoming Slack events."""
    reload_settings()
    text = str(event.get("text") or "").strip()
    channel_id = str(event.get("channel") or "")
    user_id = str(event.get("user") or "")
    thread_ts = str(event.get("thread_ts") or "")  # only set if message is already inside a thread

    if not channel_id or not text or not user_id:
        return

    log.info(
        "Processing Slack event: user=%s channel=%s thread_ts=%s text=%r (bot_configured=%s)",
        user_id,
        channel_id,
        thread_ts or "None (top-level)",
        text[:80],
        bool(settings.slack_bot_token),
    )

    slack_client = SlackClient(settings)

    # 1. Check if the thread corresponds to an active Jira ticket in DB
    ticket_found = False
    if thread_ts:
        try:
            store = PromptStore()
            replier = Workflow2Replier(settings=settings, prompt_store=store)
            res = replier.reply(
                Workflow2ReplyRequest(
                    slack_thread_ts=thread_ts,
                    slack_channel_id=channel_id,
                    user_message=text,
                    user_id=user_id,
                )
            )
            bot_reply = str(res.get("reply") or "").strip()
            if bot_reply:
                slack_client.post_message(channel_id=channel_id, text=bot_reply, thread_ts=thread_ts)
                ticket_found = True
        except LookupError:
            ticket_found = False
        except Exception as exc:
            log.warning("Workflow2Replier error in slack event: %s", exc)

    if ticket_found:
        return

    # Determine reply target: if already in thread, reply in thread; for app_mentions, start thread under the mention; otherwise post to channel/thread
    target_thread = thread_ts or (event.get("ts") if event.get("type") == "app_mention" else None)

    # 2. Check if message references a Jira issue key like PROJ-123
    import re
    jira_keys = re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", text)
    if jira_keys:
        issue_key = jira_keys[0]
        log.info("Detected Jira ticket key in Slack message: %s", issue_key)
        try:
            from app.jira_client import JiraClient
            jira_client = JiraClient(settings)
            if jira_client.is_configured():
                issue = jira_client.get_issue(issue_key)
                fields = issue.get("fields") or {}
                summary = fields.get("summary", "")
                status_name = (fields.get("status") or {}).get("name", "Open")

                llm = build_llm_client(settings)
                prompt = (
                    f"User message: {text}\n\n"
                    f"Jira Ticket: {issue_key}\n"
                    f"Summary: {summary}\n"
                    f"Status: {status_name}\n"
                )
                bot_reply = llm.complete(
                    system_prompt=(
                        "You are AI Governor, an autonomous Scrum Master and delivery governance agent on Slack.\n"
                        "FORMATTING RULES:\n"
                        "- Keep responses short, crisp, and direct.\n"
                        "- DO NOT use markdown tables (| col |). Use clean bullet points (•) and bold headers (*text*).\n"
                        "- Highlight key ticket facts (status, summary, next steps) cleanly."
                    ),
                    user_message=prompt,
                    max_tokens=500,
                ).strip()
                slack_client.post_message(channel_id=channel_id, text=bot_reply, thread_ts=target_thread)
                return
        except Exception as exc:
            log.warning("Failed processing Jira key lookup: %s", exc)

    # 3. Fallback: General AI Governor Scrum Master conversational reply with live Jira context
    try:
        jira_context = ""
        if settings.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
                with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
                    rows = conn.execute(
                        "SELECT ticket_key, summary, status, priority, assignee_name FROM jira_ticket_cache ORDER BY updated_at DESC NULLS LAST LIMIT 15"
                    ).fetchall()
                    if not rows and settings.jira_base_url and settings.jira_email and settings.jira_api_token:
                        from app.jira_fetcher import fetch_all_tickets
                        fetch_all_tickets(force_refresh=True)
                        rows = conn.execute(
                            "SELECT ticket_key, summary, status, priority, assignee_name FROM jira_ticket_cache ORDER BY updated_at DESC NULLS LAST LIMIT 15"
                        ).fetchall()
                    if rows:
                        lines = [f"• *{r['ticket_key']}*: {r['summary']} — *{r['status'] or 'Open'}* (Assignee: {r['assignee_name'] or 'Unassigned'})" for r in rows]
                        jira_context = "\n\nLive Jira Tickets in Workspace:\n" + "\n".join(lines)
            except Exception as j_exc:
                log.warning("Could not fetch jira cache context for slack event: %s", j_exc)

        llm = build_llm_client(settings)
        prompt = f"User message: {text}{jira_context}"
        bot_reply = llm.complete(
            system_prompt=(
                "You are AI Governor, an autonomous Scrum Master and delivery governor assisting the team on Slack.\n\n"
                "CRITICAL FORMATTING & TONE RULES FOR SLACK:\n"
                "1. Keep responses SHORT, CRISP, and cleanly structured.\n"
                "2. NEVER output markdown tables (| col | col |). Slack does not render tables; use bullet points (•) and bold (*text*).\n"
                "3. If greeted (e.g. 'Hello', 'Hi'), introduce yourself in 2-3 brief bullet points explaining how you help with sprint tracking and blockers.\n"
                "4. When asked about tickets, list ONLY the real Jira tickets provided in the workspace context above with their key, summary, and status.\n"
                "5. No filler text or verbose explanations. Get straight to actionable answers."
            ),
            user_message=prompt,
            max_tokens=500,
        ).strip()
        slack_client.post_message(channel_id=channel_id, text=bot_reply, thread_ts=target_thread)
    except Exception as exc:
        log.exception("Failed to send conversational Slack reply: %s", exc)


@router.post("/slack/events")
@router.post("/api/slack/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Any:
    """Slack Events API webhook endpoint.
    
    Handles:
    - url_verification: Returns challenge back to Slack for URL validation.
    - event_callback: Dispatches message / app_mention processing to background tasks.
    """
    try:
        body = await request.json()
    except Exception:
        log.warning("Invalid JSON received at /slack/events")
        return {"ok": False, "error": "invalid_json"}

    event_type = body.get("type")

    # 1. Handle URL Verification challenge
    if event_type == "url_verification":
        challenge = body.get("challenge")
        log.info("Received Slack url_verification challenge: %s", challenge)
        return {"challenge": challenge}

    # 2. Handle Event Callback
    if event_type == "event_callback":
        event = body.get("event", {})
        # Filter out bot messages, bot subtypes, and message edits to prevent infinite loops
        if (
            event.get("bot_id")
            or event.get("subtype") in ("bot_message", "message_changed", "message_deleted")
            or event.get("bot_profile")
        ):
            return {"ok": True, "status": "ignored_bot"}

        # Dispatch background processing task
        background_tasks.add_task(
            _process_slack_event_async,
            event=event,
            team_id=body.get("team_id"),
        )
        return {"ok": True, "status": "enqueued"}

    return {"ok": True, "status": "unhandled_event_type"}

