import { useState, useRef, useEffect } from "react";
import "./App.css";

const STATUS_MESSAGES = [
  { delay: 0,     text: "Connecting to GitHub…" },
  { delay: 4000,  text: "Searching repositories…" },
  { delay: 12000, text: "Aggregating user skill matches…" },
  { delay: 22000, text: "Fetching READMEs and profile data…" },
  { delay: 38000, text: "Generating AI summaries via Gemini…" },
  { delay: 56000, text: "Ranking developers and building PDF…" },
  { delay: 72000, text: "Almost done — finalising report…" },
];

function App() {
  const [skillInput, setSkillInput] = useState("");
  const [skills, setSkills]         = useState([]);
  const [count, setCount]           = useState(5);
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState("");
  const [success, setSuccess]       = useState(false);
  const [statusMsg, setStatusMsg]   = useState("");

  const timersRef  = useRef([]);
  const inputRef   = useRef(null);

  const clearTimers = () => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  };
  useEffect(() => () => clearTimers(), []);

  // Add skill on Enter or comma
  const handleSkillKeyDown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addSkill();
    }
  };

  const addSkill = () => {
    const val = skillInput.trim().toLowerCase().replace(/,/g, "");
    if (!val) return;
    if (skills.includes(val)) {
      setSkillInput("");
      return;
    }
    setSkills((prev) => [...prev, val]);
    setSkillInput("");
  };

  const removeSkill = (skill) => {
    setSkills((prev) => prev.filter((s) => s !== skill));
  };

  const startStatusCycle = () => {
    clearTimers();
    STATUS_MESSAGES.forEach(({ delay, text }) => {
      const t = setTimeout(() => setStatusMsg(text), delay);
      timersRef.current.push(t);
    });
  };

  const generate = async () => {
    setError("");
    setSuccess(false);
    setStatusMsg("");

    // Flush any partially typed skill
    const pendingSkill = skillInput.trim().toLowerCase().replace(/,/g, "");
    let finalSkills = skills;
    if (pendingSkill && !skills.includes(pendingSkill)) {
      finalSkills = [...skills, pendingSkill];
      setSkills(finalSkills);
      setSkillInput("");
    }

    if (finalSkills.length === 0) {
      setError("Please add at least one skill.");
      return;
    }

    const n = Number(count);
    if (!n || n < 1) {
      setError("Count must be at least 1.");
      return;
    }

    setLoading(true);
    startStatusCycle();

    try {
      const res = await fetch("http://localhost:8000/generate-resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skills: finalSkills, count: n }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error: ${res.status}`);
      }

      const blob = await res.blob();
      const url  = window.URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = "github_report.pdf";
      a.click();
      window.URL.revokeObjectURL(url);
      setSuccess(true);
      setStatusMsg("");
    } catch (err) {
      setError(err.message || "Something went wrong. Please try again.");
      setStatusMsg("");
    } finally {
      clearTimers();
      setLoading(false);
    }
  };

  return (
    <div className="page">
      <div className="container">
        <div className="header">
          <h1>GitHub Developer Finder</h1>
        </div>

        <div className="card">

          {/* ── Skills field ── */}
          <div className="field">
            <label>Skills</label>

            {/* Tag display box */}
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
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}

            {/* Input box */}
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
                aria-label="Add skill"
              >
                Add
              </button>
            </div>
          </div>

          {/* ── Count field ── */}
          <div className="field">
            <label>
              Number of Developers
            </label>
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
      </div>
    </div>
  );
}

export default App;
