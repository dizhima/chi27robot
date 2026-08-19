import type { Strategy, StrategyId } from "./types";

type StrategySelectorProps = {
  strategies: Strategy[];
  selectedStrategyId: StrategyId | null;
  onSelect: (strategyId: StrategyId) => void;
};

export function StrategySelector({
  strategies,
  selectedStrategyId,
  onSelect,
}: StrategySelectorProps) {
  return (
    <section className="authoring-strategy-section">
      <div className="authoring-section-heading">
        <div>
          <span className="authoring-eyebrow">Team organization</span>
          <h2>Strategy</h2>
        </div>
      </div>
      <div className="authoring-strategies">
        {strategies.map((strategy) => (
          <button
            type="button"
            className="authoring-strategy"
            aria-pressed={selectedStrategyId === strategy.id}
            key={strategy.id}
            onClick={() => onSelect(strategy.id)}
          >
            <span className="authoring-strategy-title">
              {strategy.label}
              {strategy.recommended ? <span>Recommended</span> : null}
            </span>
            <span>{strategy.description}</span>
            <small>{strategy.rationale}</small>
          </button>
        ))}
      </div>
    </section>
  );
}
