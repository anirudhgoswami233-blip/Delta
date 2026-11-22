"use client";

/**
 * Agent control bar: watch a session live or replay a past one, without a terminal.
 *
 * Talks to the race agent's local control service through the Delta backend
 * (/api/live/control/*). Live turns on by itself when a session's live window
 * opens and nothing else is running; during a replay it offers a switch instead.
 */

import { useEffect, useMemo, useState } from "react";

import {
  agentControl,
  type AgentSessionRef,
  type AgentSettings,
  type AgentStatus,
} from "@/lib/api";

const SPEEDS = [1, 2, 4, 10, 20];

function when(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function relative(iso?: string | null): string {
  if (!iso) return "";
  const minutes = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (Math.abs(minutes) < 1) return "now";
  const abs = Math.abs(minutes);
  const text = abs < 60 ? `${abs} min` : abs < 48 * 60 ? `${Math.floor(abs / 60)} h ${abs % 60} min` : `${Math.round(abs / 1440)} days`;
  return minutes > 0 ? `in ${text}` : `${text} ago`;
}

function sessionTitle(s?: AgentSessionRef | null): string {
  if (!s) return "";
  return `${s.meeting_name ?? ""} · ${s.session_name ?? ""}`.trim();
}

export function AgentControl() {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pane, setPane] = useState<"live" | "replay" | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      try {
        const s = await agentControl.status();
        if (!stopped) {
          setStatus(s);
          setError(null);
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      } finally {
        if (!stopped) timer = setTimeout(run, 3000);
      }
    };
    run();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (!showLog) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      try {
        const res = await agentControl.log(80);
        if (!stopped) setLog(res.lines ?? []);
      } catch {
        /* keep the last lines */
      } finally {
        if (!stopped) timer = setTimeout(run, 3000);
      }
    };
    run();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [showLog]);

  const job = status?.job ?? null;
  const shown = pane ?? (job?.kind === "replay" ? "replay" : "live");

  const act = async (fn: () => Promise<AgentStatus>) => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const settings = status?.settings;
  const saveSettings = (patch: Partial<AgentSettings>) => {
    if (!settings) return;
    act(() => agentControl.settings({ ...settings, ...patch }));
  };

  if (status && !status.available) {
    return (
      <section className="panel border-signal-warn/40 px-3 py-2">
        <p className="text-xs text-signal-warn">Agent control unavailable</p>
        <p className="mt-0.5 text-2xs leading-relaxed text-ink-muted">{status.reason}</p>
      </section>
    );
  }

  const live = status?.live_session;
  const disabled = busy || !!status?.pending;

  return (
    <section className="panel">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-graphite-700 px-3 py-2">
        <div className="flex overflow-hidden rounded border border-graphite-600" role="tablist" aria-label="Source">
          {(["live", "replay"] as const).map((key) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={shown === key}
              onClick={() => setPane(key)}
              className={`px-3 py-1 text-2xs font-bold uppercase tracking-[0.14em] ${
                shown === key
                  ? key === "live" ? "bg-signal-alert/15 text-signal-alert" : "bg-signal-info/15 text-signal-info"
                  : "text-ink-faint hover:text-ink-muted"
              }`}
            >
              {key === "live" ? "● Live" : "▶ Replay"}
            </button>
          ))}
        </div>

        <span className="text-2xs text-ink-muted">
          {status?.pending ? (
            <span className="text-signal-info">{status.pending}…</span>
          ) : job?.kind === "live" ? (
            <><span className="font-semibold text-signal-alert">Live agent running</span>{job.params.session ? ` · ${sessionTitle(job.params.session)}` : ""}</>
          ) : job?.kind === "replay" ? (
            <><span className="font-semibold text-signal-info">Replaying</span> {sessionTitle(job.params.session)} at {job.params.speed}x</>
          ) : (
            "Idle"
          )}
        </span>

        {settings && (
          <div className="ml-auto flex flex-wrap items-center gap-3 text-2xs text-ink-faint">
            <label className="flex items-center gap-1.5" title="Claude explains the session. Off = readings only, no cost.">
              <input type="checkbox" checked={settings.claude} disabled={disabled} onChange={(e) => saveSettings({ claude: e.target.checked })} />
              Claude
            </label>
            <select
              className="rounded border border-graphite-600 bg-graphite-850 px-1.5 py-0.5 text-2xs text-ink"
              value={settings.model}
              disabled={disabled || !settings.claude}
              onChange={(e) => saveSettings({ model: e.target.value as AgentSettings["model"] })}
              title="Claude model for explanations"
            >
              <option value="sonnet">Sonnet</option>
              <option value="opus">Opus</option>
              <option value="haiku">Haiku</option>
            </select>
            <label className="flex items-center gap-1.5" title={status?.radio_transcription ? "Transcribe team radio and identify who is speaking" : "faster-whisper is not installed"}>
              <input type="checkbox" checked={settings.radio} disabled={disabled || !status?.radio_transcription} onChange={(e) => saveSettings({ radio: e.target.checked })} />
              Radio transcripts
            </label>
            <button type="button" className="text-ink-faint underline-offset-2 hover:text-ink-muted hover:underline" onClick={() => setShowLog((v) => !v)}>
              {showLog ? "Hide log" : "Log"}
            </button>
          </div>
        )}
      </header>

      {/* Banners */}
      {status?.token && !status.token.valid && (
        <Banner tone="warn">
          F1TV login needed for live data (your token is missing or expired).
          <button type="button" className="btn ml-2" disabled={busy || status.login_in_progress} onClick={() => act(agentControl.login)}>
            {status.login_in_progress ? "Login window open on this Mac…" : "Log in with F1TV"}
          </button>
        </Banner>
      )}
      {status?.live_available && job?.kind === "replay" && live && (
        <Banner tone="alert">
          <span className="mr-1 inline-block h-2 w-2 animate-pulse-dot rounded-full bg-signal-alert" />
          <b className="text-ink">{sessionTitle(live)}</b> is live now.
          <button type="button" className="btn ml-2 border-signal-alert/50 text-signal-alert" disabled={disabled} onClick={() => { setPane("live"); act(agentControl.startLive); }}>
            Switch to live
          </button>
        </Banner>
      )}
      {error && <Banner tone="alert">{error}</Banner>}

      <div className="px-3 py-2.5">
        {shown === "live" ? (
          <LivePane status={status} disabled={disabled} act={act} saveSettings={saveSettings} />
        ) : (
          <ReplayPane status={status} disabled={disabled} act={act} />
        )}
      </div>

      {showLog && (
        <pre className="num max-h-56 overflow-auto border-t border-graphite-700 bg-graphite-950 px-3 py-2 text-[10px] leading-relaxed text-ink-faint">
          {log.length ? log.join("\n") : "No log yet."}
        </pre>
      )}
    </section>
  );
}

function Banner({ tone, children }: { tone: "warn" | "alert" | "info"; children: React.ReactNode }) {
  const cls = {
    warn: "border-signal-warn/30 bg-signal-warn/10 text-signal-warn",
    alert: "border-signal-alert/30 bg-signal-alert/10 text-signal-alert",
    info: "border-signal-info/30 bg-signal-info/10 text-signal-info",
  }[tone];
  return <div className={`flex flex-wrap items-center gap-1 border-b px-3 py-1.5 text-xs ${cls}`}>{children}</div>;
}

function LivePane({
  status,
  disabled,
  act,
  saveSettings,
}: {
  status: AgentStatus | null;
  disabled: boolean;
  act: (fn: () => Promise<AgentStatus>) => void;
  saveSettings: (patch: Partial<AgentSettings>) => void;
}) {
  const job = status?.job;
  const live = status?.live_session;
  const next = status?.next_session;
  const auto = status?.settings?.auto_live ?? true;
  const running = job?.kind === "live";

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
      <div className="min-w-[260px] flex-1 text-xs leading-relaxed text-ink-muted">
        {running ? (
          <>
            <p className="text-ink">
              Recording the official live timing feed with your F1TV account and explaining it as it happens.
            </p>
            <p className="text-2xs text-ink-faint">
              Started {when(job.started_at)}{job.params.reason === "auto" ? " automatically" : ""} ·{" "}
              {job.params.claude ? `Claude ${job.params.model}` : "Claude off"}
              {!live && " · no session in its live window: the agent waits for the next one"}
            </p>
          </>
        ) : live ? (
          <p>
            <b className="text-ink">{sessionTitle(live)}</b> is in its live window now (starts {when(live.date_start)}).
            {status?.auto_live_suppressed && " Auto-live is paused for this session because you stopped it or chose a replay."}
          </p>
        ) : (
          <p>
            No session is live right now.
            {next && (
              <>
                {" "}Next: <b className="text-ink">{sessionTitle(next)}</b>, {when(next.date_start)} ({relative(next.date_start)}).
                {auto ? ` Live turns on automatically from ${when(next.window_opens)}.` : " Auto-live is off."}
              </>
            )}
          </p>
        )}
      </div>
      <label className="flex items-center gap-1.5 text-2xs text-ink-muted" title="Start live by itself when a session's live window opens (60 min before a race, 15 min before other sessions), unless a replay is running">
        <input type="checkbox" checked={auto} disabled={disabled} onChange={(e) => saveSettings({ auto_live: e.target.checked })} />
        Turn live on automatically
      </label>
      {running ? (
        <button type="button" className="btn" disabled={disabled} onClick={() => act(agentControl.stop)} title="Stops gracefully: the final explanation and debrief are written first">
          Stop live
        </button>
      ) : (
        <button type="button" className="btn btn-primary" disabled={disabled || !status?.token?.valid} onClick={() => act(agentControl.startLive)}>
          {job?.kind === "replay" ? "Stop replay and go live" : "Start live"}
        </button>
      )}
    </div>
  );
}

function ReplayPane({
  status,
  disabled,
  act,
}: {
  status: AgentStatus | null;
  disabled: boolean;
  act: (fn: () => Promise<AgentStatus>) => void;
}) {
  const [sessions, setSessions] = useState<AgentSessionRef[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [year, setYear] = useState<number | null>(null);
  const [meeting, setMeeting] = useState<number | null>(null);
  const [sessionKey, setSessionKey] = useState<number | null>(null);
  const [speed, setSpeed] = useState(4);
  const [fromMinute, setFromMinute] = useState("0");
  const [untilMinute, setUntilMinute] = useState("");

  useEffect(() => {
    agentControl
      .sessions()
      .then((res) => {
        setSessions(res.sessions ?? []);
        const first = res.sessions?.[0];
        if (first) {
          setYear(first.year ?? null);
          setMeeting(first.meeting_key ?? null);
          setSessionKey(first.session_key);
        }
      })
      .catch((e) => setLoadError((e as Error).message));
  }, []);

  const years = useMemo(() => [...new Set(sessions.map((s) => s.year).filter(Boolean))] as number[], [sessions]);
  const meetings = useMemo(() => {
    const seen = new Map<number, string>();
    for (const s of sessions) if (s.year === year && s.meeting_key != null && !seen.has(s.meeting_key)) seen.set(s.meeting_key, s.meeting_name ?? String(s.meeting_key));
    return [...seen.entries()];
  }, [sessions, year]);
  const meetingSessions = useMemo(
    () => sessions.filter((s) => s.meeting_key === meeting).sort((a, b) => (a.date_start ?? "").localeCompare(b.date_start ?? "")),
    [sessions, meeting],
  );
  const chosen = sessions.find((s) => s.session_key === sessionKey);

  const from = Number(fromMinute) || 0;
  const until = untilMinute.trim() === "" ? null : Number(untilMinute);
  const span = (until ?? chosen?.duration_minutes ?? 120) - from;
  const realMinutes = Math.max(0, span / speed);
  const job = status?.job;

  const field = "rounded border border-graphite-600 bg-graphite-850 px-1.5 py-1 text-xs text-ink";

  if (loadError) return <p className="text-xs text-signal-alert">{loadError}</p>;

  return (
    <div className="flex flex-col gap-2">
      {job?.kind === "replay" && (
        <p className="text-xs text-ink-muted">
          Replaying <b className="text-ink">{sessionTitle(job.params.session)}</b> at {job.params.speed}x from minute{" "}
          {job.params.from_minute}{job.params.until_minute != null ? ` to ${job.params.until_minute}` : ""}.{" "}
          <span className="text-2xs text-ink-faint">A past session played through the same pipeline as live. It is never labelled live.</span>
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-0.5">
          <span className="field-label">Season</span>
          <select className={field} value={year ?? ""} onChange={(e) => { const y = Number(e.target.value); setYear(y); const m = sessions.find((s) => s.year === y); setMeeting(m?.meeting_key ?? null); setSessionKey(m?.session_key ?? null); }}>
            {years.map((y) => <option key={y} value={y}>{y}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="field-label">Grand Prix</span>
          <select className={field} value={meeting ?? ""} onChange={(e) => { const m = Number(e.target.value); setMeeting(m); const s = sessions.filter((x) => x.meeting_key === m); setSessionKey((s.find((x) => x.session_type === "Race") ?? s[0])?.session_key ?? null); }}>
            {meetings.map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="field-label">Session</span>
          <select className={field} value={sessionKey ?? ""} onChange={(e) => setSessionKey(Number(e.target.value))}>
            {meetingSessions.map((s) => <option key={s.session_key} value={s.session_key}>{s.session_name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="field-label">Speed</span>
          <select className={field} value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
            {SPEEDS.map((s) => <option key={s} value={s}>{s}x</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5" title="Minutes after the session start (negative = before the start)">
          <span className="field-label">From minute</span>
          <input className={`${field} w-20`} type="number" value={fromMinute} onChange={(e) => setFromMinute(e.target.value)} />
        </label>
        <label className="flex flex-col gap-0.5" title="Leave empty to play to the end">
          <span className="field-label">To minute</span>
          <input className={`${field} w-20`} type="number" placeholder="end" value={untilMinute} onChange={(e) => setUntilMinute(e.target.value)} />
        </label>
        <button
          type="button"
          className="btn btn-primary"
          disabled={disabled || !sessionKey || (until != null && until <= from)}
          onClick={() => sessionKey && act(() => agentControl.startReplay({ session_key: sessionKey, speed, from_minute: from, until_minute: until }))}
        >
          {job?.kind === "live" ? "Stop live and replay" : job?.kind === "replay" ? "Restart replay" : "Start replay"}
        </button>
        {job?.kind === "replay" && (
          <button type="button" className="btn" disabled={disabled} onClick={() => act(agentControl.stop)}>
            Stop
          </button>
        )}
      </div>
      <p className="text-2xs text-ink-faint">
        {chosen ? `${sessionTitle(chosen)}, ${when(chosen.date_start)}. ` : ""}
        Plays for about {realMinutes < 1 ? "<1" : Math.round(realMinutes)} min
        {status?.settings?.claude ? `, with Claude ${status.settings.model} explaining as it goes` : ", readings only (Claude off)"}.
        {!sessions.length && " No finished sessions in the schedule yet."}
      </p>
    </div>
  );
}
