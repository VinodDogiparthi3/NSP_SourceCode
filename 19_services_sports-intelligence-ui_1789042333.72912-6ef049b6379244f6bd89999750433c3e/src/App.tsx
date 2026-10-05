import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Activity, AlertTriangle, ArrowUp, Bot, ChevronDown, Database, Gauge,
  History, Menu, MessageSquareText, PanelLeftClose, Plus, RotateCcw,
  Search, ShieldCheck, Sparkles, Target, ThumbsDown, ThumbsUp,
} from "lucide-react";
import { getDropoutAlerts, getHealth, streamChat } from "./api";
import type { ChatMessage, DropoutAlert, DropoutResponse, HealthStatus, SourceRecord } from "./types";

const fallbackSuggestions = [
  "Which active athletes show the highest early-warning dropout risk?",
  "How many active athletes are registered by sport and region?",
  "Show Saudi medal trends by sport and year from SOPC",
  "Find Taekwondo competition results and explain the performance",
];
const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
const HISTORY_KEY = "sports-intelligence-history-v1";
const makeId = () => crypto.randomUUID();

type SavedChat = {
  id: string;
  title: string;
  messages: ChatMessage[];
  conversationId: string | null;
  updatedAt: number;
};

function loadHistory(): SavedChat[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]").slice(0, 12);
  } catch {
    return [];
  }
}

function SourcePanel({ sources, latencyMs }: { sources: SourceRecord[]; latencyMs?: number }) {
  const [open, setOpen] = useState(false);
  return <div className="sources">
    <button className="sources-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
      <span><Database size={15} /> {sources.length} grounded evidence item{sources.length === 1 ? "" : "s"}</span>
      <span className="source-meta">{latencyMs ? `${latencyMs} ms` : ""}<ChevronDown size={16} /></span>
    </button>
    {open && <div className="source-list">{sources.map((source, index) =>
      <article className="source-card" key={`${source.record_id}-${index}`}>
        <div className="source-topline">
          <span className="record-number">Evidence {index + 1}</span>
          <span className={`role-badge ${source.source.toLowerCase()}`}>{source.source.replace("_", " ")}</span>
          <span className="similarity">{source.distance === 0 ? "Exact data" : `${Math.max(0, (1 - source.distance) * 100).toFixed(0)}% match`}</span>
        </div>
        <strong className="source-title">{source.title}</strong>
        <p dir="auto">{source.document_text}</p>
      </article>)}</div>}
  </div>;
}

function MessageText({ message }: { message: ChatMessage }) {
  if (message.role === "user") return <>{message.text}</>;
  return <div className="markdown-answer"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text}</ReactMarkdown></div>;
}

function RiskCard({ alert }: { alert: DropoutAlert }) {
  const percent = Math.round(alert.dropout_probability * 100);
  return <article className="risk-card">
    <div className="risk-card-head">
      <div><span className="athlete-label">Athlete ID</span><strong>{alert.athlete_id}</strong></div>
      <span className={`risk-badge ${alert.risk_level.toLowerCase()}`}>{percent}% · {alert.risk_level} RISK</span>
    </div>
    <div className="risk-grid">
      <div><span>Sport</span><strong>{alert.sport || "Unknown"}</strong></div>
      <div><span>Region</span><strong>{alert.region || "Unknown"}</strong></div>
      <div><span>Federation</span><strong>{alert.federation || "Unknown"}</strong></div>
      <div><span>Last activity</span><strong>{alert.days_since_activity} days ago</strong></div>
    </div>
    <div className="risk-progress"><span style={{ width: `${percent}%` }} /></div>
    <div className="signals"><span>Signals</span><ul>{alert.signals.map((signal) => <li key={signal}>{signal}</li>)}</ul></div>
    <div className="action"><Target size={15} /><div><span>Recommended action</span><strong>{alert.recommended_action}</strong></div></div>
  </article>;
}

function EarlyWarning({ data, loading }: { data: DropoutResponse | null; loading: boolean }) {
  if (loading) return <div className="loading-panel"><Activity className="spin" /> Loading early-warning results…</div>;
  if (!data?.ready) return <div className="empty-panel"><AlertTriangle /><h2>Early-warning model is not ready</h2></div>;
  const summary = data.summary;
  return <section className="warning-page">
    <div className="metric-grid">
      <div><Activity /><span>Active athletes monitored</span><strong>{Number(summary.total_monitored || 0).toLocaleString()}</strong></div>
      <div><AlertTriangle /><span>High-risk proxy alerts</span><strong>{Number(summary.high_risk || 0).toLocaleString()}</strong></div>
      <div><Gauge /><span>Medium-risk monitoring</span><strong>{Number(summary.medium_risk || 0).toLocaleString()}</strong></div>
      <div><History /><span>Data snapshot</span><strong>{summary.snapshot_date || "—"}</strong></div>
    </div>
    <div className="alert-heading"><div><span>Weekly alert sample</span><h2>Highest-priority outreach</h2></div><span>{data.alerts.length} anonymised alerts shown</span></div>
    <div className="risk-list">{data.alerts.map((alert) => <RiskCard key={alert.athlete_id} alert={alert} />)}</div>
  </section>;
}

function OpeningScreen() {
  return <div className="opening-screen">
    <div className="opening-glow" />
    <div className="opening-content">
      <div className="opening-mark"><Sparkles /></div>
      <h1>Sports Intelligence</h1>
      <p>NSP + SOPC decision support</p>
      <div className="opening-loader"><span /></div>
      <small>Preparing your intelligence workspace</small>
    </div>
  </div>;
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [view, setView] = useState<"chat" | "risk">("chat");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const [savedChats, setSavedChats] = useState<SavedChat[]>(loadHistory);
  const [historySearch, setHistorySearch] = useState("");
  const [suggestions, setSuggestions] = useState(fallbackSuggestions);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [dropout, setDropout] = useState<DropoutResponse | null>(null);
  const [riskLoading, setRiskLoading] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setBooting(false), 1400);
    getHealth().then(setHealth).catch(() => {});
    getDropoutAlerts(20).then(setDropout).catch(() => {}).finally(() => setRiskLoading(false));
    fetch(`${API_BASE}/api/suggestions`).then((r) => r.json()).then((d) => setSuggestions(d.suggestions)).catch(() => {});
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => { localStorage.setItem(HISTORY_KEY, JSON.stringify(savedChats.slice(0, 12))); }, [savedChats]);
  useEffect(() => {
    if (!currentChatId || messages.length === 0) return;
    setSavedChats((items) => items.map((item) => item.id === currentChatId
      ? { ...item, messages, conversationId, updatedAt: Date.now() }
      : item));
  }, [messages, conversationId, currentChatId]);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const updateAssistant = (id: string, updater: (message: ChatMessage) => ChatMessage) =>
    setMessages((current) => current.map((message) => message.id === id ? updater(message) : message));

  const startNewChat = () => {
    if (streaming) return;
    setMessages([]); setConversationId(null); setCurrentChatId(null); setInput(""); setView("chat"); setSidebarOpen(false);
    window.setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const openSavedChat = (chat: SavedChat) => {
    if (streaming) return;
    setCurrentChatId(chat.id); setConversationId(chat.conversationId); setMessages(chat.messages); setView("chat"); setSidebarOpen(false);
  };

  const submit = async (text = input) => {
    const question = text.trim();
    if (!question || streaming) return;
    if (!currentChatId) {
      const chatId = makeId();
      setCurrentChatId(chatId);
      setSavedChats((items) => [{ id: chatId, title: question.slice(0, 46), messages: [], conversationId: null, updatedAt: Date.now() }, ...items].slice(0, 12));
    }
    const assistantId = makeId();
    const conversationHistory = messages
      .filter((message) => message.text.trim() && !message.error)
      .slice(-12)
      .map((message) => ({ role: message.role, text: message.text.slice(0, 6000) }));
    setInput(""); setStreaming(true);
    setMessages((current) => [...current, { id: makeId(), role: "user", text: question }, { id: assistantId, role: "assistant", text: "", streaming: true }]);
    try {
      await streamChat(question, conversationId, conversationHistory, {
        onRoute: (route, dataset) => updateAssistant(assistantId, (m) => ({ ...m, route, dataset })),
        onRetrieval: (sources, retrievalMs) => updateAssistant(assistantId, (m) => ({ ...m, sources, retrievalMs })),
        onAnalysis: (sql) => updateAssistant(assistantId, (m) => ({ ...m, sql })),
        onToken: (token) => updateAssistant(assistantId, (m) => ({ ...m, text: m.text + token })),
        onDone: (id) => { setConversationId(id); updateAssistant(assistantId, (m) => ({ ...m, streaming: false })); setStreaming(false); },
        onError: (error) => { updateAssistant(assistantId, (m) => ({ ...m, text: error, error: true, streaming: false })); setStreaming(false); },
      });
    } catch {
      updateAssistant(assistantId, (m) => ({ ...m, text: "I couldn't reach the local intelligence service. Check that the backend is running.", error: true, streaming: false }));
      setStreaming(false);
    }
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
  };
  const filteredChats = savedChats.filter((chat) => chat.title.toLowerCase().includes(historySearch.toLowerCase()));

  if (booting) return <OpeningScreen />;

  return <div className="workspace-shell">
    {sidebarOpen && <button className="sidebar-scrim" aria-label="Close menu" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
      <div className="sidebar-brand">
        <div className="brand-mark"><Sparkles size={18} /></div>
        <div><strong>Sports Intelligence</strong><span>NSP + SOPC</span></div>
        <button className="collapse-button" onClick={() => setSidebarOpen(false)} aria-label="Close menu"><PanelLeftClose /></button>
      </div>
      <button className="new-chat" onClick={startNewChat}><Plus /> New conversation</button>
      <nav className="sidebar-nav" aria-label="Workspace">
        <span className="nav-label">Workspace</span>
        <button className={view === "chat" ? "active" : ""} onClick={() => { setView("chat"); setSidebarOpen(false); }}><MessageSquareText /> Ask the data</button>
        <button className={view === "risk" ? "active" : ""} onClick={() => { setView("risk"); setSidebarOpen(false); }}><AlertTriangle /> Early warning</button>
      </nav>
      <section className="history-panel">
        <div className="history-heading"><span>Recent conversations</span><History /></div>
        <label className="history-search"><Search /><input value={historySearch} onChange={(e) => setHistorySearch(e.target.value)} placeholder="Search history" /></label>
        <div className="history-list">
          {filteredChats.length === 0 ? <p className="history-empty">Your conversations will appear here.</p> : filteredChats.map((chat) =>
            <button key={chat.id} className={currentChatId === chat.id ? "active" : ""} onClick={() => openSavedChat(chat)}>
              <MessageSquareText /><span>{chat.title}</span>
            </button>)}
        </div>
      </section>
      <div className="sidebar-status">
        <div><span className={`status-dot ${health?.gemini_configured ? "" : "warning"}`} /><strong>{health?.gemini_configured ? "AI systems ready" : "AI setup required"}</strong></div>
        <p>{Number(health?.nsp_record_count || 132021).toLocaleString()} NSP · {Number(health?.sopc_record_count || 9296).toLocaleString()} SOPC</p>
      </div>
    </aside>

    <section className="workspace-main">
      <header className="workspace-header">
        <div><button className="menu-button" onClick={() => setSidebarOpen(true)} aria-label="Open menu"><Menu /></button><div><span>{view === "chat" ? "Conversational intelligence" : "Use case 1"}</span><strong>{view === "chat" ? (currentChatId ? savedChats.find((item) => item.id === currentChatId)?.title || "Ask the data" : "Ask the data") : "Athlete dropout early warning"}</strong></div></div>
        <button className="header-new-chat" onClick={startNewChat}><RotateCcw /> New chat</button>
      </header>

      {view === "risk" ? <main className="wide-main"><EarlyWarning data={dropout} loading={riskLoading} /></main> : <>
        <main className={`chat ${messages.length === 0 ? "empty" : ""}`}>
          {messages.length === 0 ? <section className="hero compact-hero">
            <div className="hero-orb"><Sparkles /></div>
            <h1>What would you like to explore?</h1>
            <p>Ask about athletes, participation, federations, performance, medals, or early-warning risk.</p>
            <div className="suggestion-grid">{suggestions.map((suggestion) => <button key={suggestion} onClick={() => submit(suggestion)}><MessageSquareText /><p>{suggestion}</p><ArrowUp /></button>)}</div>
          </section> : <section className="message-list" aria-live="polite">
            {messages.map((message) => <div className={`message-row ${message.role}`} key={message.id}>
              {message.role === "assistant" && <div className="avatar"><Bot size={17} /></div>}
              <div className="message-content">
                {message.role === "assistant" && message.route && <div className="route-line"><span>{message.route.replaceAll("_", " ")}</span>{message.dataset && <span>{message.dataset}</span>}</div>}
                <div className={`bubble ${message.error ? "error" : ""}`} dir="auto">
                  {message.role === "assistant" && !message.text && message.streaming ? <div className="searching"><span /><span /><span /> Understanding and checking local data…</div> : <MessageText message={message} />}
                  {message.streaming && message.text && <span className="cursor" />}
                </div>
                {message.role === "assistant" && message.sources && message.sources.length > 0 && <SourcePanel sources={message.sources} latencyMs={message.retrievalMs} />}
                {message.role === "assistant" && message.sql && <details className="sql-details"><summary>Exact analytics query</summary><code>{message.sql}</code></details>}
                {message.role === "assistant" && !message.streaming && !message.error && message.text && <div className="feedback"><button aria-label="Helpful"><ThumbsUp /></button><button aria-label="Not helpful"><ThumbsDown /></button></div>}
              </div>
            </div>)}<div ref={bottomRef} />
          </section>}
        </main>
        <footer className="composer-wrap"><form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); submit(); }}><textarea ref={textareaRef} value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="Message Sports Intelligence" rows={1} dir="auto" disabled={streaming} aria-label="Message Sports Intelligence" /><button type="submit" disabled={!input.trim() || streaming} aria-label="Send message"><ArrowUp /></button></form><p><ShieldCheck /> Grounded in local NSP and SOPC data · Verify high-impact decisions</p></footer>
      </>}
    </section>
  </div>;
}
