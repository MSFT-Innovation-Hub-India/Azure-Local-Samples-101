import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const payload = JSON.parse(text);
      detail = typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status}).`;
    } catch {
      // Middleware errors can be plain text.
    }
    throw new Error(detail || `Request failed (${response.status}).`);
  }
  return response.json();
}

function App() {
  const [status, setStatus] = useState(null);
  const [scenarios, setScenarios] = useState([]);
  const [selected, setSelected] = useState("operations");
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef(null);
  const revision = useRef(null);

  async function refresh() {
    setBusy(true);
    try {
      const [catalog, next] = await Promise.all([
        request("/api/scenarios"),
        request(`/api/status?scenario=${encodeURIComponent(selected)}`),
      ]);
      if (revision.current && revision.current !== next.scenario.revision) setMessages([]);
      revision.current = next.scenario.revision;
      setScenarios(catalog.scenarios);
      setStatus(next);
      setError("");
    } catch (failure) {
      setStatus(null);
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { refresh(); }, []);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  async function send(event) {
    event.preventDefault();
    const text = question.trim();
    if (!text || busy || !status?.indexed) return;
    setBusy(true);
    setError("");
    try {
      const answer = await request("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": status.csrf_token },
        body: JSON.stringify({ question: text, scenario: selected }),
      });
      const turns = [
        { role: "user", content: text },
        {
          role: "assistant", content: answer.answer, sources: answer.sources,
          usage: answer.usage, modelCalled: answer.model_called,
        },
      ];
      const sameRevision = revision.current === answer.revision;
      setMessages((previous) => sameRevision ? [...previous, ...turns] : turns);
      revision.current = answer.revision;
      setQuestion("");
    } catch (failure) {
      setError(`${failure.message} This turn was not saved; you can retry. Token usage is unavailable for this failed request.`);
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setError("");
    try {
      await request(`/api/reset?scenario=${encodeURIComponent(selected)}`, {
        method: "POST", headers: { "X-CSRF-Token": status.csrf_token },
      });
      setMessages([]);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  async function switchScenario(id) {
    if (busy || id === selected) return;
    setBusy(true);
    setError("");
    try {
      const next = await request(`/api/status?scenario=${encodeURIComponent(id)}`);
      await request(`/api/reset?scenario=${encodeURIComponent(id)}`, {
        method: "POST", headers: { "X-CSRF-Token": next.csrf_token },
      });
      revision.current = next.scenario.revision;
      setSelected(id);
      setStatus(next);
      setMessages([]);
      setQuestion("");
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return <div className="layout">
    <aside>
      <div className="brand"><span className="mark">AL</span> AZURE LOCAL</div>
      <p className="eyebrow">PRIVATE KNOWLEDGE</p>
      <h1>Your documents.<br />Your infrastructure.</h1>
      <p>Ask questions about your PDFs. Retrieval, embeddings, and model inference run on the appliance.</p>
      <section className="status">
        <span className={`dot ${status?.indexed ? "ready" : ""}`} />
        {status ? (status.indexed ? `${status.chunks} indexed passages` : "Waiting for PDF indexing") : "Checking services"}
      </section>
      <button className="secondary" onClick={refresh} disabled={busy}>Refresh index status</button>
      <div className="architecture">
        <p>React + FastAPI</p><p>Local embeddings + Qdrant</p><p>Foundry Local + Qwen</p>
      </div>
      <p className="footnote">Demo access uses the Azure Arc tunnel. This is not a disconnected deployment.</p>
    </aside>
    <main>
      <header>
        <div><h2>PDF Assistant</h2><p>{status?.scenario.name || "Grounded answers, with source passages"}</p></div>
        <div className="header-actions">
          <label className="scenario-picker" htmlFor="scenario">Use case scenario
            <select id="scenario" value={selected} disabled={busy || !scenarios.length}
              onChange={(event) => switchScenario(event.target.value)}>
              {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.name}</option>)}
            </select>
          </label>
          <button className="secondary" onClick={refresh} disabled={busy}>Refresh scenarios</button>
          <button className="secondary" onClick={reset} disabled={busy || !status}>New conversation</button>
        </div>
      </header>
      <div className="conversation" aria-live="polite">
        {!messages.length && <section className="welcome">
          <div className="welcome-icon">?</div>
          <h2>What would you like to find?</h2>
          <p>{status?.scenario.description || "Ask a specific question about the indexed PDFs, then explore the source passages below each answer."}</p>
          <div className="examples">
            {(status?.scenario.example_questions || []).map((text) =>
              <button key={text} onClick={() => setQuestion(text)}>{text}</button>)}
          </div>
          <p className="footnote">The small Qwen model can make mistakes. Check the sources before relying on an answer.</p>
        </section>}
        {messages.map((message, index) => <article key={index} className={`message ${message.role}`}>
          <span className="role">{message.role === "user" ? "YOU" : "ASSISTANT"}</span>
          <div className="answer">{message.content}</div>
          {message.role === "assistant" && <p className="token-usage">
            {message.modelCalled === false
              ? "No model call - no supporting passages; no inference tokens used."
              : message.usage
                ? `Tokens (model-reported): Input ${message.usage.prompt_tokens} | Output ${message.usage.completion_tokens} | Total ${message.usage.total_tokens}`
                : "Token usage unavailable - model did not report counts."}
          </p>}
          {!!message.sources?.length && <div className="sources">
            <p className="source-label">Retrieved sources - verify the answer against these passages</p>
            {message.sources.map((source) => <details key={source.id}>
              <summary>[{source.id}] {source.document} <span>Page {source.page}</span></summary>
              <p>{source.excerpt}</p>
            </details>)}
          </div>}
        </article>)}
        {busy && <p className="working" role="status">Working locally. CPU inference may take a few minutes...</p>}
        <div ref={end} />
      </div>
      <footer>
        {error && <div role="alert" className="error">{error}</div>}
        {status && !status.indexed && <p className="notice">Publish documents for this scenario, then refresh index status.</p>}
        <form onSubmit={send}>
          <label className="sr-only" htmlFor="question">Question about your PDFs</label>
          <textarea id="question" value={question} maxLength={1000} rows={2}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask a question about your PDFs..."
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(event); }
            }} disabled={busy} />
          <button type="submit" disabled={busy || !question.trim() || !status?.indexed}>Ask</button>
        </form>
        <p className="footnote">History is isolated by scenario. Switching scenarios starts a new conversation. Sessions expire after one hour.</p>
      </footer>
    </main>
  </div>;
}

createRoot(document.getElementById("root")).render(<App />);
