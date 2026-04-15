import { useState, useRef, useEffect } from "react";
import "./App.css";

function App() {
  const [skillInput, setSkillInput] = useState("");
  const [skills, setSkills]         = useState([]);
  const [count, setCount]           = useState(5);
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState("");
  const [success, setSuccess]       = useState(false);
  const [statusMsg, setStatusMsg]   = useState("");

  const inputRef    = useRef(null);
  const readerRef   = useRef(null);   // holds the stream reader so we can cancel

  // Cancel stream on unmount
  useEffect(() => () => readerRef.current?.cancel(), []);

  // ── Skill tag logic ────────────────────────────────────────────────────────
  const handleSkillKeyDown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addSkill();
    }
  };

  const addSkill = () => {
    const val = skillInput.trim().toLowerCase().replace(/,/g, "");
    if (!val || skills.includes(val)) { setSkillInput(""); return; }
    setSkills((prev) => [...prev, val]);
    setSkillInput("");
  };

  const removeSkill = (skill) =>
    setSkills((prev) => prev.filter((s) => s !== skill));

  // ── Generate ───────────────────────────────────────────────────────────────
  const generate = async () => {
    setError("");
    setSuccess(false);
    setStatusMsg("");

    // Flush partially typed skill
    const pending = skillInput.trim().toLowerCase().replace(/,/g, "");
    let finalSkills = skills;
    if (pending && !skills.includes(pending)) {
      finalSkills = [...skills, pending];
      setSkills(finalSkills);
      setSkillInput("");
    }

    if (finalSkills.length === 0) { setError("Please add at least one skill."); return; }
    const n = Number(count);
    if (!n || n < 1) { setError("Count must be at least 1."); return; }

    setLoading(true);

    try {
      const res = await fetch("http://localhost:8000/generate-resume", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ skills: finalSkills, count: n }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error: ${res.status}`);
      }

      // ── Read SSE stream ──────────────────────────────────────────────────
      const reader  = res.body.getReader();
      readerRef.current = reader;
      const decoder = new TextDecoder();
      let   buffer  = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE messages are separated by double newlines
        const parts = buffer.split("\n\n");
        buffer = parts.pop();           // keep incomplete tail

        for (const part of parts) {
          if (!part.trim()) continue;

          // Parse  "event: xxx\ndata: {...}"
          const eventMatch = part.match(/^event:\s*(\S+)/m);
          const dataMatch  = part.match(/^data:\s*(.+)/m);
          if (!eventMatch || !dataMatch) continue;

          const event   = eventMatch[1];
          let   payload = {};
          try { payload = JSON.parse(dataMatch[1]); } catch { continue; }

          if (event === "status") {
            setStatusMsg(payload.message);

          } else if (event === "done") {
            // Decode base64 PDF and trigger download
            const bytes  = Uint8Array.from(atob(payload.pdf), (c) => c.charCodeAt(0));
            const blob   = new Blob([bytes], { type: "application/pdf" });
            const url    = URL.createObjectURL(blob);
            const a      = document.createElement("a");
            a.href       = url;
            a.download   = "github_report.pdf";
            a.click();
            URL.revokeObjectURL(url);
            setSuccess(true);
            setStatusMsg("");

          } else if (event === "error") {
            throw new Error(payload.detail || "An error occurred.");
          }
        }
      }

    } catch (err) {
      setError(err.message || "Something went wrong. Please try again.");
      setStatusMsg("");
    } finally {
      readerRef.current = null;
      setLoading(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="page">
      <div className="container">
        <div className="header">
          <div className="logo">⚡</div>
          <h1>GitHub Developer Finder</h1>
          <p className="subtitle">
            Search GitHub by skills, rank developers, and export a PDF report.
          </p>
        </div>

        <div className="card">

          {/* Skills */}
          <div className="field">
            <label>Skills</label>
            {skills.length > 0 && (
              <div className="tag-box">
                {skills.map((s) => (
                  <span key={s} className="tag">
                    {s}
                    <button
                      className="tag-remove"
                      onClick={() => removeSkill(s)}
                      disabled={loading}
                      aria-label={`Remove ${s}`}
                    >×</button>
                  </span>
                ))}
              </div>
            )}
            <div className="skill-input-row">
              <input
                ref={inputRef}
                className="input"
                placeholder="Type a skill and press Enter"
                value={skillInput}
                onChange={(e) => setSkillInput(e.target.value)}
                onKeyDown={handleSkillKeyDown}
                disabled={loading}
              />
              <button
                className="add-btn"
                onClick={addSkill}
                disabled={loading || !skillInput.trim()}
              >Add</button>
            </div>
          </div>

          {/* Count */}
          <div className="field">
            <label>Number of Developers</label>
            <input
              className="input input-number"
              type="number"
              min={1}
              value={count}
              onChange={(e) => setCount(e.target.value)}
              disabled={loading}
            />
          </div>

          {error   && <div className="alert error">{error}</div>}
          {success && <div className="alert success">✅ PDF downloaded successfully!</div>}

          {/* Live status from backend */}
          {loading && statusMsg && (
            <div className="status-bar">
              <span className="spinner" />
              {statusMsg}
            </div>
          )}

          <button className="btn" onClick={generate} disabled={loading}>
            {loading ? "Generating…" : "Generate Report"}
          </button>
        </div>

        <p className="footer">Powered by GitHub API · Google Gemini · FastAPI</p>
      </div>
    </div>
  );
}

export default App;
