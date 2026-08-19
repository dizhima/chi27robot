import type { ActuatedJointInfo } from "mujoco-react";

const ARM_JOINT = /^robot(\d+)_joint([1-7])$/;
const ARM_ACTUATOR = /^robot(\d+)_torq_j([1-7])$/;

export const DEFAULT_READY_POSE_GAINS = {
  kp: [80, 80, 60, 60, 30, 20, 15],
  kd: [12, 12, 10, 8, 5, 4, 3],
} as const;

export type ReadyPoseJoint = {
  robot: string;
  robotIndex: number;
  jointIndex: number;
  jointName: string;
  actuatorName: string;
  qposAdr: number;
  dofAdr: number;
  ctrlAdr: number;
  ctrlRange: readonly [number, number];
};

export type ReadyPoseDiscovery = {
  joints: ReadyPoseJoint[];
  warnings: string[];
};

type ParsedArmName = { robotIndex: number; jointIndex: number };

function parseArmName(name: string, pattern: RegExp): ParsedArmName | null {
  const match = pattern.exec(name);
  if (!match) return null;
  const robotIndex = Number(match[1]);
  const jointIndex = Number(match[2]);
  return Number.isInteger(robotIndex) && Number.isInteger(jointIndex)
    ? { robotIndex, jointIndex }
    : null;
}

function validAddress(value: number): boolean {
  return Number.isInteger(value) && value >= 0;
}

function validRange(range: readonly number[]): range is readonly [number, number] {
  return range.length === 2
    && Number.isFinite(range[0])
    && Number.isFinite(range[1])
    && range[0] < range[1];
}

/**
 * Select complete, scalar, direct-torque Panda arm mappings from the public
 * `mujoco-react` actuator introspection result. Invalid robots are skipped as
 * a whole, so a malformed robot never receives partial control.
 */
export function discoverReadyPoseJoints(actuatedJoints: readonly ActuatedJointInfo[]): ReadyPoseDiscovery {
  type Candidate = { info: ActuatedJointInfo; robotIndex: number; jointIndex: number };
  const robots = new Map<number, Candidate[]>();

  for (const info of actuatedJoints) {
    const joint = parseArmName(info.name, ARM_JOINT);
    const actuator = parseArmName(info.actuatorName, ARM_ACTUATOR);
    if (!joint && !actuator) continue;
    if (!joint || !actuator || joint.robotIndex !== actuator.robotIndex || joint.jointIndex !== actuator.jointIndex) {
      // Keep a synthetic group only to produce one actionable warning.
      const robotIndex = joint?.robotIndex ?? actuator?.robotIndex;
      if (robotIndex !== undefined) robots.set(robotIndex, []);
      continue;
    }
    const list = robots.get(joint.robotIndex) ?? [];
    list.push({ info, robotIndex: joint.robotIndex, jointIndex: joint.jointIndex });
    robots.set(joint.robotIndex, list);
  }

  const joints: ReadyPoseJoint[] = [];
  const warnings: string[] = [];
  for (const robotIndex of [...robots.keys()].sort((a, b) => a - b)) {
    const candidates = robots.get(robotIndex) ?? [];
    const byJoint = new Map<number, Candidate>();
    let reason: string | null = null;
    for (const candidate of candidates) {
      if (byJoint.has(candidate.jointIndex)) {
        reason = "duplicate arm actuator mapping";
        break;
      }
      const { info } = candidate;
      if (info.typeName !== "hinge") {
        reason = "arm joint is not a scalar hinge";
        break;
      }
      if (!validAddress(info.qposAdr) || !validAddress(info.dofAdr) || !validAddress(info.ctrlAdr)) {
        reason = "invalid MuJoCo address";
        break;
      }
      if (!validRange(info.ctrlRange)) {
        reason = "unusable actuator control range";
        break;
      }
      byJoint.set(candidate.jointIndex, candidate);
    }
    if (!reason && byJoint.size !== 7) reason = "requires all seven arm joint/actuator pairs";
    if (!reason) {
      const uniqueQpos = new Set([...byJoint.values()].map(({ info }) => info.qposAdr));
      const uniqueDof = new Set([...byJoint.values()].map(({ info }) => info.dofAdr));
      const uniqueCtrl = new Set([...byJoint.values()].map(({ info }) => info.ctrlAdr));
      if (uniqueQpos.size !== 7 || uniqueDof.size !== 7 || uniqueCtrl.size !== 7) {
        reason = "duplicate MuJoCo address";
      }
    }
    if (reason) {
      warnings.push(`[ready-pose] skipping robot${robotIndex}: ${reason}.`);
      continue;
    }
    for (let jointIndex = 1; jointIndex <= 7; jointIndex++) {
      const candidate = byJoint.get(jointIndex)!;
      joints.push({
        robot: `robot${robotIndex}`,
        robotIndex,
        jointIndex,
        jointName: candidate.info.name,
        actuatorName: candidate.info.actuatorName,
        qposAdr: candidate.info.qposAdr,
        dofAdr: candidate.info.dofAdr,
        ctrlAdr: candidate.info.ctrlAdr,
        ctrlRange: candidate.info.ctrlRange,
      });
    }
  }
  return { joints, warnings };
}

export function readyPoseTorque(
  joint: Pick<ReadyPoseJoint, "jointIndex" | "ctrlRange">,
  targetQpos: number,
  qpos: number,
  qvel: number,
  qfrcBias: number,
): number {
  if (![targetQpos, qpos, qvel, qfrcBias].every(Number.isFinite)) return 0;
  const index = joint.jointIndex - 1;
  const kp = DEFAULT_READY_POSE_GAINS.kp[index];
  const kd = DEFAULT_READY_POSE_GAINS.kd[index];
  if (!Number.isFinite(kp) || !Number.isFinite(kd) || !validRange(joint.ctrlRange)) return 0;
  const torque = qfrcBias + kp * (targetQpos - qpos) - kd * qvel;
  if (!Number.isFinite(torque)) return 0;
  return Math.min(joint.ctrlRange[1], Math.max(joint.ctrlRange[0], torque));
}
