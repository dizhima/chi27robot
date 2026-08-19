import type { ObjectGoalTask, Robot, RobotId } from "./types";

type SemanticTaskListProps = {
  tasks: ObjectGoalTask[];
  robots: Robot[];
  onReassign: (taskId: string, robotId: RobotId) => void;
  onEditTarget: (taskId: string) => void;
  onDelete: (taskId: string) => void;
};

export function SemanticTaskList({
  tasks,
  robots,
  onReassign,
  onEditTarget,
  onDelete,
}: SemanticTaskListProps) {
  return (
    <section className="authoring-tasks-section">
      <div className="authoring-section-heading">
        <div>
          <span className="authoring-eyebrow">Semantic plan</span>
          <h2>Tasks</h2>
        </div>
        <span className="authoring-count">{tasks.length} active</span>
      </div>
      <div className="authoring-task-groups">
        {robots.map((robot) => {
          const assignedTasks = tasks.filter((task) => task.assignee === robot.id);
          return (
            <section className="authoring-task-group" key={robot.id}>
              <header>
                <strong>{robot.label}</strong>
                <span>{assignedTasks.length} tasks</span>
              </header>
              {assignedTasks.map((task) => (
                <article className="authoring-task" data-testid="semantic-task" key={task.id}>
                  <strong>{task.object.name}</strong>
                  <div className="authoring-task-goal">
                    <span>{task.relation}</span>
                    <button type="button" onClick={() => onEditTarget(task.id)}>
                      {task.target.name}
                    </button>
                  </div>
                  <label>
                    <span className="sr-only">Assign {task.object.name}</span>
                    <select
                      aria-label={`Assign ${task.object.name}`}
                      value={task.assignee}
                      onChange={(event) => onReassign(task.id, event.target.value as RobotId)}
                    >
                      {robots.map((candidate) => (
                        <option value={candidate.id} key={candidate.id}>
                          {candidate.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="authoring-delete"
                    aria-label={`Delete ${task.object.name}`}
                    onClick={() => onDelete(task.id)}
                  >
                    Delete
                  </button>
                </article>
              ))}
            </section>
          );
        })}
      </div>
    </section>
  );
}
