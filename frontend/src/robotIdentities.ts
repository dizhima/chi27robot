type BodyIdentity = {
  id: number;
  name: string;
  parentId: number;
};

export type RobotIdentityAnchor = {
  robot: string;
  rootBodyId: number;
  trackingBodyId: number;
  bodyIds: number[];
};

const ROBOT_BODY_NAME = /^(robot\d+)_/;

/**
 * Discover namespaced robots without depending on body ids from a particular XML.
 * `robotN_base` is preferred; otherwise the shallowest body in that namespace is
 * used, which keeps the overlay useful for assets with a different root suffix.
 */
export function discoverRobotIdentityAnchors(
  bodies: readonly BodyIdentity[],
): RobotIdentityAnchor[] {
  const byRobot = new Map<string, BodyIdentity[]>();
  const byId = new Map(bodies.map((body) => [body.id, body]));

  for (const body of bodies) {
    const robot = ROBOT_BODY_NAME.exec(body.name)?.[1];
    if (!robot) continue;
    const entries = byRobot.get(robot) ?? [];
    entries.push(body);
    byRobot.set(robot, entries);
  }

  const depthOf = (body: BodyIdentity) => {
    let depth = 0;
    let current = body;
    const visited = new Set<number>();
    while (current.parentId !== current.id && !visited.has(current.id)) {
      visited.add(current.id);
      const parent = byId.get(current.parentId);
      if (!parent) break;
      depth += 1;
      current = parent;
    }
    return depth;
  };

  return [...byRobot.entries()]
    .map(([robot, entries]) => {
      const preferred = entries.find((body) => body.name === `${robot}_base`);
      const root = preferred ?? [...entries].sort((a, b) => depthOf(a) - depthOf(b) || a.id - b.id)[0];
      // RoboCasa's robotN_base can be a fixed outer frame while navigation
      // joints move the nested arm/base assembly. link0 follows that assembly,
      // so prefer it for the label's world XY and retain a generic fallback.
      const tracking = entries.find((body) => body.name === `${robot}_link0`) ?? root;
      return {
        robot,
        rootBodyId: root.id,
        trackingBodyId: tracking.id,
        bodyIds: entries.map((body) => body.id),
      };
    })
    .sort((a, b) => {
      const ai = Number(a.robot.slice("robot".length));
      const bi = Number(b.robot.slice("robot".length));
      return ai - bi;
    });
}
