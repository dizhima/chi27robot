import { useState, type KeyboardEvent } from "react";

type ScenePathPickerProps = {
  id: string;
  ariaLabel: string;
  value: string;
  paths: readonly string[];
  onChange: (value: string) => void;
  onConfirm: () => void;
};

export function ScenePathPicker({
  id,
  ariaLabel,
  value,
  paths,
  onChange,
  onConfirm,
}: ScenePathPickerProps) {
  const [open, setOpen] = useState(false);
  const optionsId = `${id}-options`;

  const handleInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      onConfirm();
    } else if (event.key === "Escape") {
      setOpen(false);
    } else if (event.key === "ArrowDown" && !open) {
      event.preventDefault();
      setOpen(true);
    }
  };

  return (
    <div
      className="scene-path-picker"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setOpen(false);
        }
      }}
    >
      <input
        id={id}
        aria-label={ariaLabel}
        aria-controls={optionsId}
        aria-expanded={open}
        aria-autocomplete="list"
        role="combobox"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleInputKeyDown}
      />
      <button
        type="button"
        className="scene-path-picker-toggle"
        aria-label="Show preset scene paths"
        aria-controls={optionsId}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        ▾
      </button>
      {open ? (
        <div id={optionsId} className="scene-path-picker-options" role="listbox">
          {paths.map((path) => (
            <button
              type="button"
              role="option"
              aria-selected={path === value}
              key={path}
              onClick={() => {
                onChange(path);
                setOpen(false);
              }}
            >
              {path}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
