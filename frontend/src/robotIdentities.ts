type BodyIdentity = {
  id: number;
  name: string;
  parentId: number;
};

export type RobotIdentityDescriptor = {
  index?: number;
  namespace?: string;
  root_body?: string;
  tracking_body?: string;
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
  descriptors?: Readonly<Record<string, RobotIdentityDescriptor>>,
): RobotIdentityAnchor[] {
  const byRobot = new Map<string, BodyIdentity[]>();
  const byId = new Map(bodies.map((body) => [body.id, body]));
  const descriptorBackedRobots = new Set<string>();

  if (descriptors && Object.keys(descriptors).length > 0) {
    for (const [robot, descriptor] of Object.entries(descriptors)) {
      const namespace = descriptor.namespace;
      const entries = bodies.filter((body) =>
        body.name === descriptor.root_body
        || body.name === descriptor.tracking_body
        || (namespace != null && (
          body.name === namespace || body.name.startsWith(`${namespace}_`)
        )),
      );
      if (entries.length > 0) {
        byRobot.set(robot, entries);
        descriptorBackedRobots.add(robot);
      }
    }
  }

  for (const body of bodies) {
    const robot = ROBOT_BODY_NAME.exec(body.name)?.[1];
    if (!robot) continue;
    // A manifest descriptor is authoritative when present. This fallback
    // keeps older scenes and tests without robot descriptors working.
    if (descriptorBackedRobots.has(robot)) continue;
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
      const descriptor = descriptors?.[robot];
      const preferred = entries.find((body) =>
        body.name === descriptor?.root_body,
      ) ?? entries.find((body) => body.name === `${robot}_base`);
      const root = preferred ?? [...entries].sort((a, b) => depthOf(a) - depthOf(b) || a.id - b.id)[0];
      // RoboCasa's robotN_base can be a fixed outer frame while navigation
      // joints move the nested arm/base assembly. link0 follows that assembly,
      // so prefer it for the label's world XY and retain a generic fallback.
      const tracking = entries.find((body) =>
        body.name === descriptor?.tracking_body,
      ) ?? entries.find((body) => body.name === `${robot}_link0`) ?? root;
      return {
        robot,
        rootBodyId: root.id,
        trackingBodyId: tracking.id,
        bodyIds: entries.map((body) => body.id),
      };
    })
    .sort((a, b) => {
      const ai = descriptors?.[a.robot]?.index;
      const bi = descriptors?.[b.robot]?.index;
      if (ai != null || bi != null) {
        return (ai ?? Number.MAX_SAFE_INTEGER) - (bi ?? Number.MAX_SAFE_INTEGER);
      }
      return Number(a.robot.slice("robot".length)) - Number(b.robot.slice("robot".length));
    });
}
