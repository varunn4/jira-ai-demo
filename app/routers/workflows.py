"""FastAPI Router for Jira AI Workflows (WF1 - WF8, Test Cases, PR Gate)."""

from __future__ import annotations

import logging
from typing import Any
from fastapi import APIRouter, HTTPException

from app.config import settings
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
