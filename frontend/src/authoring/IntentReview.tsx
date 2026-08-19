type IntentReviewProps = {
  summary: string;
  onChange: (value: string) => void;
  onConfirm: () => void;
};

export function IntentReview({ summary, onChange, onConfirm }: IntentReviewProps) {
  return (
    <section className="authoring-intent-review">
      <div className="authoring-section-heading">
        <div>
          <span className="authoring-eyebrow">Intent review</span>
          <h2>Intent</h2>
        </div>
        <span className="authoring-status">Ready to review</span>
      </div>
      <textarea
        className="authoring-intent-summary"
        aria-label="Intent"
        value={summary}
        rows={4}
        onChange={(event) => onChange(event.target.value)}
      />
      <p className="authoring-help">
        Continue the conversation in Codex CLI if this does not capture your goal.
      </p>
      <div className="authoring-actions">
        <button
          type="button"
          className="authoring-primary"
          disabled={!summary.trim()}
          onClick={onConfirm}
        >
          Confirm intent
        </button>
      </div>
    </section>
  );
}
