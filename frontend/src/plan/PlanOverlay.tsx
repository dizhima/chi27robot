/**
 * In-scene visualization of a plan's spatial anchors: the standoff (where the
 * base dwells), the navigate route (floor polyline), and the place drop point.
 * Rendered as children of <MujocoCanvas>, so positions are MuJoCo world coords
 * directly (the scene is Z-up with no root rotation) — a floor marker sits at
 * z≈0 and rings lie flat in the XY plane (THREE ring/plane geometry default).
 *
 * Standoff is read-only (backend-derived). The place drop point and the navigate
 * route's intermediate points can be promoted to editable via-points:
 *  - drag the place ring → new drop XY;
 *  - drag a middle route node → move it; click it (no drag) → delete it;
 *  - click a segment's "+" handle → insert a waypoint at that midpoint.
 * The route's start is backend-owned; an editable destination uses its chassis
 * endpoint as the drag handle and maps that delta back to the backend's mount
 * standoff contract. Edits raycast a horizontal plane at the marker's height
 * and commit the resulting route back to the authored step (which recompiles).
 * While a drag is active we disable OrbitControls via onDragStateChange.
 */
import { useState } from "react";
import { Line } from "@react-three/drei";
import { DoubleSide } from "three";
import type { ThreeEvent } from "@react-three/fiber";
import { colorForRobot } from "../robotVisuals";
import { mountStandoffForChassisDrag } from "./standoffFrames";

/** A free-standing pin marker (a picked scene-reference position), not tied to
 *  any plan step. Rendered with the same look as a place drop point. */
export type PinMarker = {
  id: string;
  /** World point [x, y, z] on the picked surface. */
  at: [number, number, number];
};

/** One step's spatial anchors, extracted from the compiled `completed` step. */
export type StepMarker = {
  id: string;
  robot: string;
  op: string;
  /** Temporary compiler repair; editable, but not promoted to authored path intent. */
  isDetour?: boolean;
  /** Base dwell point [x, y] (navigate/pick/place). */
  standoff?: [number, number];
  /** True when this standoff may be dragged (a navigate not feeding a replay). */
  standoffEditable?: boolean;
  /** Backend-derived route [[x, y], …], including start and standoff. */
  route?: [number, number][] | null;
  /** Place drop point [x, y, z] (z from the backend surface fit). */
  at?: [number, number, number];
};

type PlanOverlayProps = {
  markers: StepMarker[];
  /** Whether to render standoff rings that cannot be dragged. */
  showReadOnlyStandoffs?: boolean;
  /** Free-standing scene-reference pins (rendered like place points). */
  pins?: PinMarker[];
  /** Pin dragged: new [x, y] for this pin. */
  onDragPin?: (pinId: string, xy: [number, number]) => void;
  /** Drag committed: new [x, y] for this step's place point. */
  onDragAt?: (stepId: string, xy: [number, number]) => void;
  /** Route edited: authored intermediate constraints, or null to auto-route. */
  onSetViaPoints?: (stepId: string, viaPoints: [number, number][] | null) => void;
  /** Standoff dragged: new dwell [x, y] for a navigate step. */
  onSetStandoff?: (stepId: string, xy: [number, number]) => void;
  /** Toggled true while a marker drag is active (caller disables OrbitControls). */
  onDragStateChange?: (dragging: boolean) => void;
};

// --- tunable overlay geometry (world metres) -------------------------------
const FLOOR_Z = 0.02; // lift floor markers off the ground to avoid z-fighting
const PLACE_LIFT = 0.012; // lift the place ring off the surface so it doesn't sink in
const NODE_DRAG_EPS = 0.03; // travel below which a node press deletes it (vs. moves it)

const WAYPOINT_ARROW_R = 0.06; // route-node arrow half-width
const WAYPOINT_ARROW_LEN = 0.17; // route-node arrow length (points along travel)
const WAYPOINT_ARROW_FLAT = 0.45; // vertical squash so the arrow lies flat-ish on the floor
const WAYPOINT_GRAB_R = 0.12; // invisible grab target around a route node
const ENDPOINT_R = 0.03; // static start / standoff dot radius
const ADD_HANDLE_R: [number, number] = [0.03, 0.055]; // "+" insert handle ring inner/outer
const STANDOFF_R: [number, number] = [0.13, 0.19]; // read-only standoff ring inner/outer
const STANDOFF_HANDLE_R = 0.07; // editable-standoff filled dot handle radius
const PLACE_R: [number, number] = [0.07, 0.13]; // place ring inner/outer
const PLACE_CORE_R = 0.05; // place-point centre sphere
const PLACE_GRAB_R = 0.14; // invisible grab target around the place point
const PIN_COLOR = "#e0b341"; // scene-reference pin (not robot-owned)

type Drag =
  | { kind: "place"; stepId: string; z: number }
  | {
      kind: "standoff";
      stepId: string;
      z: number;
      chassisStart: [number, number];
      mountStart: [number, number];
    }
  | { kind: "waypoint"; stepId: string; index: number; z: number; start: [number, number] }
  | { kind: "pin"; pinId: string; z: number };

export function shouldRenderStandoff(
  marker: Pick<StepMarker, "standoff" | "standoffEditable">,
  showReadOnlyStandoffs: boolean,
): marker is Pick<StepMarker, "standoff" | "standoffEditable"> & { standoff: [number, number] } {
  return !!marker.standoff && (!!marker.standoffEditable || showReadOnlyStandoffs);
}

export function PlanOverlay({
  markers,
  showReadOnlyStandoffs = true,
  pins = [],
  onDragPin,
  onDragAt,
  onSetViaPoints,
  onSetStandoff,
  onDragStateChange,
}: PlanOverlayProps) {
  // Active drag target + the live cursor point so the marker follows before commit.
  const [drag, setDrag] = useState<Drag | null>(null);
  const [live, setLive] = useState<[number, number] | null>(null);

  const findMarker = (id: string) => markers.find((m) => m.id === id);
  const middleRoute = (m: StepMarker): [number, number][] =>
    (m.route ?? []).slice(1, -1).map(([x, y]) => [x, y]);

  const beginPlaceDrag = (m: StepMarker, e: ThreeEvent<PointerEvent>) => {
    if (!m.at) return;
    e.stopPropagation();
    setDrag({ kind: "place", stepId: m.id, z: m.at[2] });
    setLive([m.at[0], m.at[1]]);
    onDragStateChange?.(true);
  };

  const beginStandoffDrag = (m: StepMarker, e: ThreeEvent<PointerEvent>) => {
    if (!m.standoff || !m.standoffEditable) return;
    const chassisStart = m.route?.length ? m.route[m.route.length - 1] : m.standoff;
    e.stopPropagation();
    setDrag({
      kind: "standoff",
      stepId: m.id,
      z: FLOOR_Z,
      chassisStart,
      mountStart: m.standoff,
    });
    setLive(chassisStart);
    onDragStateChange?.(true);
  };

  const beginPinDrag = (pin: PinMarker, e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    setDrag({ kind: "pin", pinId: pin.id, z: pin.at[2] });
    setLive([pin.at[0], pin.at[1]]);
    onDragStateChange?.(true);
  };

  const beginWaypointDrag = (m: StepMarker, index: number, e: ThreeEvent<PointerEvent>) => {
    if (!m.route) return;
    e.stopPropagation();
    const [x, y] = m.route[index];
    setDrag({ kind: "waypoint", stepId: m.id, index, z: FLOOR_Z, start: [x, y] });
    setLive([x, y]);
    onDragStateChange?.(true);
  };

  const moveDrag = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    setLive([e.point.x, e.point.y]);
  };

  const endDrag = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    const p: [number, number] = [e.point.x, e.point.y];
    const d = drag;
    setDrag(null);
    setLive(null);
    onDragStateChange?.(false);
    if (!d) return;
    if (d.kind === "place") {
      onDragAt?.(d.stepId, p);
      return;
    }
    if (d.kind === "pin") {
      onDragPin?.(d.pinId, p);
      return;
    }
    if (d.kind === "standoff") {
      onSetStandoff?.(
        d.stepId,
        mountStandoffForChassisDrag(d.mountStart, d.chassisStart, p),
      );
      return;
    }
    // waypoint: drag = move, tap (no move) = delete.
    const m = findMarker(d.stepId);
    if (!m || !m.route) return;
    const route = middleRoute(m);
    const j = d.index - 1; // route index (endpoints excluded)
    if (j < 0 || j >= route.length) return;
    // Decide from the actual travel at release (robust to render timing), not
    // just the live flag: a real drag moves it, a tap deletes it.
    const moved = Math.hypot(p[0] - d.start[0], p[1] - d.start[1]) > NODE_DRAG_EPS;
    if (moved) {
      route[j] = p;
      onSetViaPoints?.(d.stepId, route);
    } else {
      route.splice(j, 1);
      onSetViaPoints?.(d.stepId, route.length ? route : null);
    }
  };

  const insertWaypoint = (m: StepMarker, segIndex: number, e: ThreeEvent<MouseEvent>) => {
    e.stopPropagation();
    if (!m.route) return;
    const a = m.route[segIndex];
    const b = m.route[segIndex + 1];
    const route = middleRoute(m);
    route.splice(segIndex, 0, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    onSetViaPoints?.(m.id, route);
  };

  return (
    <group>
      {markers.map((m) => {
        const color = colorForRobot(m.robot);
        const nodes: React.ReactNode[] = [];
        const wp = m.route;

        // Navigate route: floor polyline + editable middle nodes + "+" handles.
        if (wp && wp.length >= 2) {
          const dragIdx = drag?.kind === "waypoint" && drag.stepId === m.id ? drag.index : -1;
          // While dragging the standoff, the route's end point (S) follows too.
          const standoffDragging = drag?.kind === "standoff" && drag.stepId === m.id;
          const lastIdx = wp.length - 1;
          const disp = wp.map((w, i) =>
            (i === dragIdx || (standoffDragging && i === lastIdx)) && live ? live : w,
          );
          const pts = disp.map(([x, y]) => [x, y, FLOOR_Z] as [number, number, number]);
          nodes.push(
            <Line
              key="route"
              points={pts}
              color={color}
              lineWidth={m.isDetour ? 3 : 2}
              dashed
              dashSize={m.isDetour ? 0.06 : 0.12}
              gapSize={m.isDetour ? 0.05 : 0.08}
            />,
          );

          // Insert handles at each segment midpoint.
          for (let s = 0; s < disp.length - 1; s++) {
            const mx = (disp[s][0] + disp[s + 1][0]) / 2;
            const my = (disp[s][1] + disp[s + 1][1]) / 2;
            nodes.push(
              <mesh key={`add-${s}`} position={[mx, my, FLOOR_Z]} onClick={(e) => insertWaypoint(m, s, e)}>
                <ringGeometry args={[ADD_HANDLE_R[0], ADD_HANDLE_R[1], 20]} />
                <meshBasicMaterial color={color} side={DoubleSide} transparent opacity={0.5} />
              </mesh>,
            );
          }

          // Nodes: endpoints have small dots (the editable destination also gets
          // the larger standoff handle); middle nodes are draggable and tappable, shown
          // as flat cones pointing along the direction of travel (toward the next
          // point) so the route reads as directed. Cone default axis is +Y, so a
          // Z-rotation of (heading - 90°) aims it and a vertical squash lays it flat.
          for (let i = 0; i < disp.length; i++) {
            const [x, y] = disp[i];
            if (i === 0 || i === disp.length - 1) {
              nodes.push(
                <mesh key={`wp-${i}`} position={[x, y, FLOOR_Z]}>
                  <sphereGeometry args={[ENDPOINT_R, 12, 12]} />
                  <meshBasicMaterial color={color} transparent opacity={0.55} />
                </mesh>,
              );
            } else {
              const dragging = dragIdx === i;
              // Heading = incoming travel direction (previous point → this node),
              // so the arrow arrives at the node.
              const heading = Math.atan2(y - disp[i - 1][1], x - disp[i - 1][0]);
              nodes.push(
                <group
                  key={`wp-${i}`}
                  position={[x, y, FLOOR_Z + WAYPOINT_ARROW_R * WAYPOINT_ARROW_FLAT]}
                  onPointerDown={(e) => beginWaypointDrag(m, i, e)}
                >
                  {/* cone centre sits half a length behind so its tip (apex at
                      local +Y) lands on the node and its body trails back along
                      the segment just travelled */}
                  <mesh
                    position={[
                      (-Math.cos(heading) * WAYPOINT_ARROW_LEN) / 2,
                      (-Math.sin(heading) * WAYPOINT_ARROW_LEN) / 2,
                      0,
                    ]}
                    rotation={[0, 0, heading - Math.PI / 2]}
                    scale={[1, 1, WAYPOINT_ARROW_FLAT]}
                  >
                    <coneGeometry args={[WAYPOINT_ARROW_R, WAYPOINT_ARROW_LEN, 16]} />
                    <meshBasicMaterial color={dragging ? "#ffffff" : color} />
                  </mesh>
                  {/* larger invisible grab target so the node is easy to grab */}
                  <mesh>
                    <sphereGeometry args={[WAYPOINT_GRAB_R, 12, 12]} />
                    <meshBasicMaterial transparent opacity={0} depthWrite={false} />
                  </mesh>
                </group>,
              );
            }
          }
        }

        // Standoff at the base dwell point. An editable navigate shows just a
        // filled dot handle (no ring — it would double up with the pick/place
        // ring that sits at the same spot); pick/place (and replay-pinned
        // navigates) show the read-only ring.
        if (shouldRenderStandoff(m, showReadOnlyStandoffs)) {
          const dragging = drag?.kind === "standoff" && drag.stepId === m.id;
          const displayStandoff = m.standoffEditable && wp?.length
            ? wp[wp.length - 1]
            : m.standoff;
          const sx = dragging && live ? live[0] : displayStandoff[0];
          const sy = dragging && live ? live[1] : displayStandoff[1];
          if (m.standoffEditable) {
            nodes.push(
              <group key="standoff" position={[sx, sy, FLOOR_Z]} onPointerDown={(e) => beginStandoffDrag(m, e)}>
                <mesh>
                  <circleGeometry args={[STANDOFF_HANDLE_R, 24]} />
                  <meshBasicMaterial color={dragging ? "#ffffff" : color} side={DoubleSide} transparent opacity={0.9} />
                </mesh>
                {/* invisible grab target, easy to grab */}
                <mesh>
                  <sphereGeometry args={[STANDOFF_HANDLE_R * 1.8, 12, 12]} />
                  <meshBasicMaterial transparent opacity={0} depthWrite={false} />
                </mesh>
              </group>,
            );
          } else {
            nodes.push(
              <mesh key="standoff" position={[sx, sy, FLOOR_Z]}>
                <ringGeometry args={[STANDOFF_R[0], STANDOFF_R[1], 32]} />
                <meshBasicMaterial color={color} side={DoubleSide} transparent opacity={0.55} />
              </mesh>,
            );
          }
        }

        // Place drop point: draggable ring + core, hovering just above the surface.
        if (m.at) {
          const dragging = drag?.kind === "place" && drag.stepId === m.id;
          const px = dragging && live ? live[0] : m.at[0];
          const py = dragging && live ? live[1] : m.at[1];
          const pz = m.at[2] + PLACE_LIFT;
          nodes.push(
            <group key="place" position={[px, py, pz]} onPointerDown={(e) => beginPlaceDrag(m, e)}>
              <mesh>
                <ringGeometry args={[PLACE_R[0], PLACE_R[1], 32]} />
                <meshBasicMaterial color={color} side={DoubleSide} transparent opacity={0.95} />
              </mesh>
              <mesh>
                <sphereGeometry args={[PLACE_CORE_R, 16, 16]} />
                <meshBasicMaterial color={dragging ? "#ffffff" : color} />
              </mesh>
              {/* larger invisible grab target so the point is easy to grab */}
              <mesh>
                <sphereGeometry args={[PLACE_GRAB_R, 12, 12]} />
                <meshBasicMaterial transparent opacity={0} depthWrite={false} />
              </mesh>
            </group>,
          );
        }

        return <group key={m.id}>{nodes}</group>;
      })}

      {/* Free-standing scene-reference pins: same look as a place drop point,
          in a distinct (non-robot) colour, draggable to adjust. */}
      {pins.map((pin) => {
        const dragging = drag?.kind === "pin" && drag.pinId === pin.id;
        const px = dragging && live ? live[0] : pin.at[0];
        const py = dragging && live ? live[1] : pin.at[1];
        const pz = pin.at[2] + PLACE_LIFT;
        return (
          <group key={`pin-${pin.id}`} position={[px, py, pz]} onPointerDown={(e) => beginPinDrag(pin, e)}>
            <mesh>
              <ringGeometry args={[PLACE_R[0], PLACE_R[1], 32]} />
              <meshBasicMaterial color={PIN_COLOR} side={DoubleSide} transparent opacity={0.95} />
            </mesh>
            <mesh>
              <sphereGeometry args={[PLACE_CORE_R, 16, 16]} />
              <meshBasicMaterial color={dragging ? "#ffffff" : PIN_COLOR} />
            </mesh>
            {/* larger invisible grab target so the pin is easy to grab */}
            <mesh>
              <sphereGeometry args={[PLACE_GRAB_R, 12, 12]} />
              <meshBasicMaterial transparent opacity={0} depthWrite={false} />
            </mesh>
          </group>
        );
      })}

      {/* Invisible horizontal drag plane at the target's height; captures the
          cursor's world XY while dragging and commits on release. */}
      {drag ? (
        <mesh position={[0, 0, drag.z]} onPointerMove={moveDrag} onPointerUp={endDrag}>
          <planeGeometry args={[200, 200]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} side={DoubleSide} />
        </mesh>
      ) : null}
    </group>
  );
}
