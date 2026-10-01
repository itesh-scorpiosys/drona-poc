"""Vyasa POC - the agent (Section 6.5), now with a group classroom.

Run locally:
    python agent.py dev

How a class runs (both modes):
    lecture  ->  Q&A  ->  wrap-up  ->  end
The timings come from .env (CLASS_MINUTES, QA_MINUTES, WRAP_SECONDS) so the
same code runs a 3-minute test and a 45-minute class.

Group mode rules:
  - Vyasa only listens to the learner who has "the floor". Everyone else can
    talk, but Vyasa ignores it, so 15 people cannot derail the lesson.
  - Raise hand puts a learner in a queue. Hands are taken in Q&A, in order.
  - During the lecture Vyasa cold-calls a random learner now and then. The
    choice is weighted towards people called less often, so it can't be
    predicted. "No response" is saved to that learner's record.
  - At wrap-up nobody has the floor, waiting hands are told to use Cowork,
    and the goodbye cannot be interrupted, so the class never ends abruptly.

APIs used here were checked against livekit-agents 1.8.3 / livekit 1.1.18.
"""
import asyncio
import datetime
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli, inference, room_io
from livekit.plugins import anthropic, liveavatar

import prompts
import records_client

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("vyasa")
logging.basicConfig(level=logging.INFO)

AGENT_NAME = "vyasa-poc"

# Section 6.4 - voice pipeline settings. Do not change without re-checking the guide.
LLM_MODEL = "claude-sonnet-4-6"
STT_MODEL = "assemblyai/universal-3-5-pro"
TTS_MODEL = "fishaudio/s2.1-pro"
TTS_VOICE_ID = "fa4c9eb3dccc4806b382b40d61c6b10a"

AVATAR_ENABLED = os.getenv("AVATAR_ENABLED", "false").strip().lower() == "true"


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else float(default)


# ---------- class timing (all overridable in .env) ----------
CLASS_MINUTES = env_float("CLASS_MINUTES", 18)          # whole class
QA_MINUTES = env_float("QA_MINUTES", 4)                 # Q&A block before wrap-up
WRAP_SECONDS = env_float("WRAP_SECONDS", 60)            # recap and goodbye
COLD_CALL_EVERY_SECONDS = env_float("COLD_CALL_EVERY_SECONDS", 240)  # average gap, randomised
COLD_CALL_WAIT_SECONDS = env_float("COLD_CALL_WAIT_SECONDS", 12)     # time to answer
QUESTION_WAIT_SECONDS = env_float("QUESTION_WAIT_SECONDS", 15)       # time to start a question
MIN_ENDPOINTING_DELAY = env_float("MIN_ENDPOINTING_DELAY", 0.7)      # stops answers being split in two

CLASS_SECONDS = CLASS_MINUTES * 60
WRAP_START = CLASS_SECONDS - WRAP_SECONDS
QA_START = WRAP_START - QA_MINUTES * 60
if QA_START <= 0:
    raise ValueError(
        f"CLASS_MINUTES={CLASS_MINUTES} is too short for QA_MINUTES={QA_MINUTES} "
        f"plus WRAP_SECONDS={WRAP_SECONDS}. Shorten Q&A or wrap-up."
    )

VOICE_RULES = """
How you speak:
- Everything you say is read aloud. Never use markdown, asterisks, bullet points, numbered lists or headings.
- Keep each turn short: one idea, under about 40 words.
""".strip()

TIMING_RULES = """
Timing:
- The class lasts {minutes} minutes: lecture first, then about {qa} minutes of questions, then a short wrap-up.
- You will get instructions when Q&A starts and when to wrap up. Pace the lesson so the main content fits before Q&A.
""".strip()

GROUP_RULES = """
This is a live group class, not a one-to-one session:
- Learners cannot speak freely. The classroom gives one learner at a time the floor, and you only hear that learner.
- During the lecture keep teaching. Do not ask the whole group open questions, because they cannot answer aloud.
- Learners press Raise hand to ask. Hands are taken during Q&A near the end; you will be told whom to call on.
- Sometimes you will be told to cold-call a named learner. Say their name and ask one short question about what you have just taught, then stop and wait.
- When a learner answers or asks, reply to them by name in one or two sentences, say clearly if an answer is right or not, then return to the lesson.
""".strip()


# ---------- state ----------
class Learner:
    def __init__(self, identity: str, record: dict):
        self.identity = identity
        self.record = record or {}
        self.joined_at = time.monotonic()
        self.left_at: float | None = None
        self.cold_calls: list[dict] = []
        self.questions: list[str] = []
        self.unanswered: list[str] = []

    @property
    def present(self) -> bool:
        return self.left_at is None

    @property
    def past_cold_calls(self) -> int:
        return int(self.record.get("cold_call_total", 0) or 0)

    def minutes_attended(self, now: float) -> float:
        end = self.left_at if self.left_at else now
        return max(0.0, (end - self.joined_at) / 60)


class ClassState:
    def __init__(self, lesson_id: str, mode: str, slot_id: str | None):
        self.lesson_id = lesson_id
        self.mode = mode
        self.slot_id = slot_id
        self.learners: dict[str, Learner] = {}
        self.queue: list[str] = []            # raised hands, in order
        self.phase = "lecture"                # lecture | qa | wrap | ended
        self.floor: str | None = None         # identity Vyasa is listening to (group)
        self.lock = asyncio.Lock()            # one scripted interaction at a time
        self.started_at = time.monotonic()
        self.agent_idle_since = time.monotonic()
        self.waiting_answer: asyncio.Future | None = None
        self.last_cold_called: str | None = None
        self.last_questioner: str | None = None
        self.current_asker: str | None = None     # learner called on in Q&A right now
        self.asker_heard = False                  # has that learner actually asked yet
        self.wrap_handle = None
        self.class_started = False            # records are only saved once the class really began
        self.closing = False
        self.saved = False
        self.tasks: list[asyncio.Task] = []

    @property
    def group(self) -> bool:
        return self.mode == "group"

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def clock(self) -> str:
        s = int(self.elapsed())
        return f"{s // 60:02d}:{s % 60:02d}"

    def present_learners(self) -> list[Learner]:
        return [l for l in self.learners.values() if l.present]


DONT_KNOW_PHRASES = (
    "i don't know", "i dont know", "don't know", "dont know", "no idea", "not sure",
    "i'm not sure", "can you repeat", "repeat that", "repeat the question", "pass",
)


def classify_answer(answer: str | None) -> str:
    """answered | dont_know | no_response. Whether an answer is actually
    correct is not judged yet; the full text is kept for grading later."""
    if not answer:
        return "no_response"
    text = answer.lower().strip(" .!?")
    if len(text.split()) <= 8 and any(p in text for p in DONT_KNOW_PHRASES):
        return "dont_know"
    return "answered"


class VyasaAgent(Agent):
    """In a group class the model must not answer learner speech on its own,
    because it tends to answer and then deliver the rest of the lesson in one
    go. Instead each finished learner turn is handed to the classroom code,
    which asks for a short, bounded reply. One-to-one keeps normal behaviour."""

    def __init__(self, *, instructions: str, on_learner_turn=None):
        super().__init__(instructions=instructions)
        self._on_learner_turn = on_learner_turn

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if self._on_learner_turn is None:
            return
        self._on_learner_turn((new_message.text_content or "").strip())
        raise StopResponse()


ACTIVE_ATTR = "drona.vyasa"   # set on the Vyasa that is teaching this room


def is_vyasa(p: rtc.RemoteParticipant) -> bool:
    """Another Vyasa agent (the LiveAvatar face is also an agent, but it
    publishes on Vyasa's behalf and carries lk.publish_on_behalf)."""
    return (
        p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        and "lk.publish_on_behalf" not in (p.attributes or {})
    )


def is_learner(p: rtc.RemoteParticipant) -> bool:
    return p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD


def load_lesson(lesson_id: str) -> str:
    path = BASE_DIR / "content" / f"lesson_{lesson_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"No such lesson file: {path}")
    return path.read_text(encoding="utf-8")


def parse_dispatch_metadata(raw: str | None) -> dict:
    """/token puts {"learner_id","lesson_id","mode","slot_id"} in the dispatch
    metadata. In group mode learner_id is just whoever joined first, so the
    agent tracks the real class list from room participants instead."""
    if not raw:
        raise ValueError(
            "No dispatch metadata received. Did you join through the api's "
            "/token endpoint rather than connecting to LiveKit directly?"
        )
    data = json.loads(raw)
    for field in ("learner_id", "lesson_id", "mode"):
        if not data.get(field):
            raise ValueError(f"Dispatch metadata is missing '{field}': {data}")
    if data["mode"] not in ("1to1", "group"):
        raise ValueError(f"Dispatch metadata has an unknown mode: {data['mode']}")
    return data


# ---------- entrypoint ----------
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    meta = parse_dispatch_metadata(ctx.job.metadata)
    lesson_id, mode, slot_id = meta["lesson_id"], meta["mode"], meta.get("slot_id")

    # Only one Vyasa per room. A Vyasa already teaching marks itself active; a newcomer
    # that sees it leaves. If two start at the same moment, the higher identity leaves.
    await asyncio.sleep(1.0)
    me = ctx.room.local_participant.identity
    others = [p for p in list(ctx.room.remote_participants.values()) if is_vyasa(p)]
    if others and (
        any((p.attributes or {}).get(ACTIVE_ATTR) == "active" for p in others)
        or any(p.identity < me for p in others)
    ):
        log.warning("another Vyasa is already in %s - this duplicate is leaving", ctx.room.name)
        ctx.shutdown(reason="duplicate Vyasa")
        return
    try:
        await ctx.room.local_participant.set_attributes({ACTIVE_ATTR: "active"})
    except Exception:
        log.exception("could not mark this Vyasa as active")
    log.info(
        "job lesson=%s mode=%s slot=%s room=%s class=%.1fmin qa_at=%ds wrap_at=%ds",
        lesson_id, mode, slot_id, ctx.room.name, CLASS_MINUTES, QA_START, WRAP_START,
    )

    state = ClassState(lesson_id, mode, slot_id)
    lesson_markdown = load_lesson(lesson_id)

    # One-to-one keeps the learner's own history in the prompt. A group class
    # has many learners, so it starts from a neutral record.
    if mode == "1to1":
        prompt_record = await safe_get_record(meta["learner_id"])
    else:
        prompt_record = {"learner_id": "the class", "lessons": []}
    system_prompt = "\n\n".join(
        [
            prompts.build(lesson_markdown, prompt_record, mode, minutes=max(1, round(CLASS_MINUTES))),
            VOICE_RULES,
            TIMING_RULES.format(minutes=CLASS_MINUTES, qa=QA_MINUTES),
        ]
        + ([GROUP_RULES] if state.group else [])
    )

    session = AgentSession(
        llm=anthropic.LLM(model=LLM_MODEL),
        stt=inference.STT(model=STT_MODEL),
        tts=inference.TTS(model=TTS_MODEL, voice=TTS_VOICE_ID),
        turn_handling={"endpointing": {"min_delay": MIN_ENDPOINTING_DELAY}},
    )

    # ---- helpers that need session/ctx ----
    async def broadcast(payload: dict) -> None:
        try:
            await ctx.room.local_participant.publish_data(json.dumps(payload).encode("utf-8"), reliable=True)
        except Exception:
            log.exception("could not broadcast %s", payload)

    def open_floor(identity: str) -> None:
        state.floor = identity
        if state.group:
            session.room_io.set_participant(identity)
            session.input.set_audio_enabled(True)
        asyncio.create_task(broadcast({"type": "floor", "identity": identity}))
        log.info("[%s] floor -> %s", state.clock(), identity)

    def close_floor() -> None:
        if state.floor is None:
            return
        state.floor = None
        if state.group:
            session.input.set_audio_enabled(False)
        asyncio.create_task(broadcast({"type": "floor", "identity": None}))
        log.info("[%s] floor closed", state.clock())

    def agent_busy() -> bool:
        return session.agent_state in ("thinking", "speaking") or session.current_speech is not None

    async def wait_until_idle(timeout: float) -> None:
        end = time.monotonic() + timeout
        while agent_busy() and time.monotonic() < end:
            await asyncio.sleep(0.2)

    async def say_line(text: str, *, interruptible: bool = True) -> None:
        """Speak an exact sentence through TTS only. Used for classroom
        management lines ("A, go ahead") that must come out word for word."""
        handle = session.say(text, allow_interruptions=interruptible)
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("line did not finish within 30 s: %s", text)

    async def speak(instructions: str, *, interruptible: bool = True, wait: bool = True):
        handle = session.generate_reply(instructions=instructions, allow_interruptions=interruptible)
        if wait:
            try:
                await asyncio.wait_for(handle.wait_for_playout(), timeout=90)
            except asyncio.TimeoutError:
                log.warning("speech did not finish within 90 s")
        return handle

    @asynccontextmanager
    async def hold_lock(timeout: float):
        """Take the interaction lock, but never block a phase change for
        longer than `timeout` seconds."""
        got = False
        try:
            await asyncio.wait_for(state.lock.acquire(), timeout)
            got = True
        except asyncio.TimeoutError:
            log.warning("[%s] proceeding without lock after %.0f s", state.clock(), timeout)
        try:
            yield
        finally:
            if got:
                state.lock.release()

    def start_task(coro) -> None:
        state.tasks.append(asyncio.create_task(coro))

    # ---- learners joining and leaving ----
    async def add_learner(p: rtc.RemoteParticipant) -> None:
        if not is_learner(p):
            return
        existing = state.learners.get(p.identity)
        if existing:
            existing.left_at = None  # rejoined
            log.info("[%s] %s rejoined", state.clock(), p.identity)
            return
        state.learners[p.identity] = Learner(p.identity, await safe_get_record(p.identity))
        log.info("[%s] %s joined (%d in class)", state.clock(), p.identity, len(state.present_learners()))

    def on_participant_connected(p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(add_learner(p))

    def on_participant_disconnected(p: rtc.RemoteParticipant) -> None:
        learner = state.learners.get(p.identity)
        if not learner:
            return
        learner.left_at = time.monotonic()
        if p.identity in state.queue:
            state.queue.remove(p.identity)
        if state.floor == p.identity:
            close_floor()
            if state.waiting_answer and not state.waiting_answer.done():
                state.waiting_answer.set_result(None)
        log.info("[%s] %s left (%d in class)", state.clock(), p.identity, len(state.present_learners()))
        if state.group and not state.present_learners():
            start_task(end_if_still_empty())

    async def end_if_still_empty() -> None:
        await asyncio.sleep(30)
        if not state.present_learners() and not state.closing:
            await end_class("everyone left")

    ctx.room.on("participant_connected", on_participant_connected)
    ctx.room.on("participant_disconnected", on_participant_disconnected)

    # ---- hands (group) ----
    def on_data(packet: rtc.DataPacket) -> None:
        if not state.group or packet.participant is None:
            return
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        who = packet.participant.identity
        kind = payload.get("type")
        if kind == "raise_hand":
            if state.phase == "wrap" or state.closing:
                learner = state.learners.get(who)
                if learner:
                    learner.unanswered.append("Raised hand during wrap-up; no time left")
                asyncio.create_task(broadcast({"type": "lower_hand", "identity": who}))
            elif who not in state.queue:
                state.queue.append(who)
                log.info("[%s] hand raised: %s (queue %s)", state.clock(), who, state.queue)
        elif kind == "lower_hand" and who in state.queue:
            state.queue.remove(who)
            log.info("[%s] hand lowered: %s", state.clock(), who)

    ctx.room.on("data_received", on_data)

    # ---- session events ----
    def on_learner_turn(text: str) -> None:
        """A complete learner turn (group mode). Hand it to whoever is waiting."""
        if text and state.waiting_answer and not state.waiting_answer.done():
            state.waiting_answer.set_result(text)
        elif text:
            log.info("[%s] learner speech ignored (nobody waiting): %s", state.clock(), text)

    @session.on("agent_state_changed")
    def on_agent_state(ev) -> None:
        if ev.new_state in ("listening", "idle"):
            state.agent_idle_since = time.monotonic()

    # ---- interactions ----
    async def take_question(identity: str) -> None:
        state.current_asker, state.asker_heard = identity, False
        try:
            await _take_question(identity)
        finally:
            state.current_asker = None

    async def _take_question(identity: str) -> None:
        if state.phase != "qa":
            return
        learner = state.learners.get(identity)
        await broadcast({"type": "lower_hand", "identity": identity})
        nxt = state.queue[0] if state.queue else None
        loop = asyncio.get_running_loop()
        state.waiting_answer = loop.create_future()
        open_floor(identity)
        line = f"{identity}, go ahead." + (f" {nxt}, you're next." if nxt else "")
        if state.phase != "qa":
            return
        await say_line(line)
        try:
            question = await asyncio.wait_for(state.waiting_answer, QUESTION_WAIT_SECONDS)
        except asyncio.TimeoutError:
            question = None
        if question:
            state.asker_heard = True
            if learner:
                learner.questions.append(question)
            close_floor()
            if state.phase == "qa":
                await speak(
                    f'The learner {identity} asked: "{question}". Answer {identity} by name in under '
                    "60 words, then stop. Do not continue the lesson and do not ask the class anything."
                )
        else:
            close_floor()
            if state.phase == "qa":
                await say_line(f"{identity}, I didn't catch that. Raise your hand again if you still have a question.")
        close_floor()
        state.waiting_answer = None
        state.last_questioner = identity

    def choose_cold_call() -> Learner | None:
        present = state.present_learners()
        if not present:
            return None
        candidates = [l for l in present if l.identity != state.last_cold_called] or present
        weights = [1 / (1 + l.past_cold_calls + len(l.cold_calls)) for l in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]

    async def cold_call(learner: Learner) -> None:
        loop = asyncio.get_running_loop()
        state.waiting_answer = loop.create_future()
        open_floor(learner.identity)
        log.info("[%s] cold call: %s", state.clock(), learner.identity)
        await speak(
            f"Cold-call {learner.identity}. Say their name, then ask one short question "
            "about what you have just taught. Ask only the question and stop."
        )
        try:
            answer = await asyncio.wait_for(state.waiting_answer, COLD_CALL_WAIT_SECONDS)
        except asyncio.TimeoutError:
            answer = None
        entry = {"at": state.clock(), "result": classify_answer(answer), "answer": answer or ""}
        learner.cold_calls.append(entry)
        log.info("[%s] cold call result: %s -> %s", state.clock(), learner.identity, entry["result"])
        close_floor()
        if entry["result"] == "answered":
            await speak(
                f'The learner {learner.identity} answered your question with: "{answer}". In under 30 words, '
                f"tell {learner.identity} by name whether that is right and correct it briefly if not. Then stop."
            )
        elif entry["result"] == "dont_know":
            await speak(
                f"The learner {learner.identity} said they don't know. In under 30 words, kindly give the "
                "answer. Then stop."
            )
        else:
            await say_line(f"No answer from {learner.identity}. Let's keep going.")
        close_floor()
        state.waiting_answer = None
        state.last_cold_called = learner.identity

    # ---- background loops ----
    async def lecture_driver() -> None:
        """In a group, nobody can reply during the lecture, so Vyasa would fall
        silent after each turn. Nudge it on to the next part of the lesson."""
        while not state.closing:
            await asyncio.sleep(0.5)
            if state.phase != "lecture" or state.lock.locked() or agent_busy():
                continue
            if session.agent_state not in ("listening", "idle"):
                continue
            if time.monotonic() - state.agent_idle_since < 2.5:
                continue
            state.agent_idle_since = time.monotonic() + 10  # retry in ~12 s if nothing happens
            session.generate_reply(
                instructions=(
                    "Teach the next single concept of the lesson in under 50 words, then stop. "
                    "The class cannot answer aloud right now, so do not ask the group open questions."
                )
            )

    async def cold_call_loop() -> None:
        await asyncio.sleep(COLD_CALL_EVERY_SECONDS * random.uniform(0.5, 1.2))
        while not state.closing and state.phase == "lecture":
            # leave room for the answer and a reply before Q&A starts
            if QA_START - state.elapsed() < COLD_CALL_WAIT_SECONDS + 25:
                return
            learner = choose_cold_call()
            called = False
            if learner:
                async with state.lock:
                    if state.phase != "lecture":
                        return
                    await wait_until_idle(45)
                    if agent_busy():
                        log.info("[%s] cold call postponed: Vyasa still speaking", state.clock())
                    else:
                        await cold_call(learner)
                        called = True
            if called:
                await asyncio.sleep(COLD_CALL_EVERY_SECONDS * random.uniform(0.6, 1.4))
            else:
                await asyncio.sleep(5)

    async def qa_loop() -> None:
        quiet_since = time.monotonic()
        nudges = 0
        while state.phase == "qa" and not state.closing:
            too_late = WRAP_START - state.elapsed() < 20   # no time left to take a question properly
            if state.queue and not state.lock.locked() and not too_late:
                async with state.lock:
                    if state.phase != "qa" or not state.queue:
                        continue
                    # someone who just asked goes behind anyone else waiting
                    if state.queue[0] == state.last_questioner and len(state.queue) > 1:
                        state.queue.append(state.queue.pop(0))
                    await take_question(state.queue.pop(0))
                quiet_since = time.monotonic()
            elif not state.queue and nudges < 2 and time.monotonic() - quiet_since > 20 and not agent_busy():
                nudges += 1
                quiet_since = time.monotonic()
                await speak(
                    "No hands are up. In one or two sentences, give a quick recap of one key point "
                    "and remind the class they can press Raise hand to ask a question."
                )
            await asyncio.sleep(0.5)

    # ---- phases ----
    async def sleep_until(seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds - state.elapsed()))

    async def enter_qa() -> None:
        state.phase = "qa"
        await broadcast({"type": "phase", "phase": "qa", "ends_in": int(CLASS_SECONDS - state.elapsed())})
        log.info("[%s] phase -> Q&A (hands waiting: %s)", state.clock(), state.queue)
        async with hold_lock(20):
            await wait_until_idle(20)
            if state.phase != "qa":
                return
            if state.group:
                await speak(
                    "Tell the class, in one or two sentences, that it is question time: anyone with a "
                    "question should press Raise hand and you will call on them in order."
                )
            else:
                await speak("Tell the learner, in one sentence, that there are a few minutes left for questions and invite one now.")
        if state.group and state.phase == "qa":
            start_task(qa_loop())

    async def enter_wrap() -> None:
        state.phase = "wrap"
        await broadcast({"type": "phase", "phase": "wrap", "ends_in": int(CLASS_SECONDS - state.elapsed())})
        waiting = list(state.queue)
        if state.current_asker and not state.asker_heard and state.current_asker not in waiting:
            waiting.insert(0, state.current_asker)   # called on, but time ran out before they spoke
        state.queue.clear()
        for identity in waiting:
            learner = state.learners.get(identity)
            if learner:
                learner.unanswered.append("Raised hand, not reached before the end of class")
            await broadcast({"type": "lower_hand", "identity": identity})
        log.info("[%s] phase -> wrap-up (unanswered hands: %s)", state.clock(), waiting)
        # Stop listening immediately, so nothing said from now on can start a new reply.
        close_floor()
        session.input.set_audio_enabled(False)
        if state.waiting_answer and not state.waiting_answer.done():
            state.waiting_answer.set_result(None)   # releases a question or cold call that is waiting
        async with hold_lock(10):
            await wait_until_idle(8)                # let the current sentence finish if it's short
            if agent_busy():
                log.info("[%s] wrap-up: cutting off the reply in progress", state.clock())
                await session.interrupt()
            if waiting:
                names = waiting[0] if len(waiting) == 1 else ", ".join(waiting[:-1]) + " and " + waiting[-1]
                session.say(
                    f"We're out of time for live questions. {names}, please bring your question to Cowork "
                    "and we'll pick it up there.",
                    allow_interruptions=False,
                )
            state.wrap_handle = session.generate_reply(
                instructions=(
                    "Time is nearly up. Wrap up now in under 40 seconds. Give a two or three sentence recap "
                    "of the key points, mention the practice task waiting in Cowork, and say a warm goodbye. "
                    "Do not start new topics, do not ask questions, and do not invite more hands."
                ),
                allow_interruptions=False,
            )

    async def end_class(reason: str) -> None:
        if state.closing:
            return
        if state.wrap_handle is not None:
            try:
                await asyncio.wait_for(state.wrap_handle.wait_for_playout(), timeout=60)
            except asyncio.TimeoutError:
                log.warning("wrap-up speech ran past 60 s")
            await asyncio.sleep(1)
        state.closing = True
        state.phase = "ended"
        log.info("[%s] class ended: %s", state.clock(), reason)
        await broadcast({"type": "class_ended", "reason": reason})
        await save_all_records()
        if state.group:
            try:
                await ctx.delete_room()  # disconnects every learner cleanly
            except Exception:
                log.exception("could not delete room")
        ctx.shutdown(reason=f"class ended: {reason}")

    async def run_clock() -> None:
        await sleep_until(QA_START)
        start_task(enter_qa())          # runs alongside, so a slow announcement can't delay wrap-up
        await sleep_until(WRAP_START)
        await enter_wrap()
        await sleep_until(CLASS_SECONDS)
        await end_class("time")

    # ---- records ----
    async def save_all_records() -> None:
        if state.saved:
            return
        state.saved = True
        if not state.class_started:
            log.warning("class never started - not writing lesson entries")
            return
        now = time.monotonic()
        today = datetime.date.today().isoformat()

        async def save_one(learner: Learner) -> None:
            rec = learner.record
            rec.setdefault("lessons", [])
            rec["learner_id"] = learner.identity
            rec["cold_call_total"] = learner.past_cold_calls + len(learner.cold_calls)
            answered = sum(1 for c in learner.cold_calls if c["result"] == "answered")
            dont_know = sum(1 for c in learner.cold_calls if c["result"] == "dont_know")
            rec["lessons"].append(
                {
                    "lesson_id": state.lesson_id,
                    "mode": state.mode,
                    "slot_id": state.slot_id,
                    "date": today,
                    "minutes": round(learner.minutes_attended(now), 1),
                    "class_minutes": CLASS_MINUTES,
                    "covered": [],          # TODO: fill from the transcript
                    "misconceptions": [],   # TODO: fill from the transcript
                    "unanswered": learner.unanswered,
                    "questions_asked": learner.questions,
                    "cold_calls": learner.cold_calls,
                    "attentiveness": {
                        "called": len(learner.cold_calls),
                        "answered": answered,
                        "dont_know": dont_know,
                        "no_response": len(learner.cold_calls) - answered - dont_know,
                    },
                }
            )
            try:
                await records_client.put(learner.identity, rec)
                log.info("record saved for %s", learner.identity)
            except Exception:
                log.exception("failed to save record for %s - check the api is running", learner.identity)

        await asyncio.gather(*(save_one(l) for l in state.learners.values()))

    async def on_shutdown() -> None:
        for task in state.tasks:
            task.cancel()
        await save_all_records()

    ctx.add_shutdown_callback(on_shutdown)

    # ---- start ----
    # list() snapshot: learners can join while we await, which would change the dict mid-loop
    for p in list(ctx.room.remote_participants.values()):
        await add_learner(p)

    await session.start(
        room=ctx.room,
        agent=VyasaAgent(
            instructions=system_prompt,
            on_learner_turn=on_learner_turn if state.group else None,
        ),
        room_options=room_io.RoomOptions(close_on_disconnect=not state.group),
    )

    if AVATAR_ENABLED:
        avatar_id = os.getenv("LIVEAVATAR_AVATAR_ID", "")
        if not avatar_id:
            log.warning("AVATAR_ENABLED is true but LIVEAVATAR_AVATAR_ID is empty - skipping avatar")
        else:
            avatar = liveavatar.AvatarSession(avatar_id=avatar_id)
            await avatar.start(session, room=ctx.room)
            log.info("avatar session started with avatar_id=%s", avatar_id)
    else:
        log.info("AVATAR_ENABLED is false - running voice-only")

    state.started_at = time.monotonic()
    state.class_started = True
    if state.group:
        session.input.set_audio_enabled(False)  # lecture: nobody has the floor
        await broadcast({"type": "phase", "phase": "lecture", "ends_in": int(CLASS_SECONDS)})
        session.generate_reply(
            instructions=(
                "In under 60 words: greet the class, say in one sentence what today's lesson covers, and give "
                "the rules in two short sentences (press Raise hand to ask, questions are taken near the end, "
                "and you may call on anyone by name at any time). Stop there. Do not start teaching yet; "
                "the lesson continues in the next turns."
            )
        )
        start_task(lecture_driver())
        start_task(cold_call_loop())
    else:
        session.generate_reply(instructions="Greet the learner warmly and start today's lesson.")

    start_task(run_clock())


async def safe_get_record(learner_id: str) -> dict:
    try:
        return await records_client.get(learner_id) or {}
    except Exception:
        log.exception("could not load record for %s - starting fresh", learner_id)
        return {}


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))