import { Navigate, Route, Routes } from "react-router-dom";
import DebugApp from "./DebugApp";
import ScenePage from "./ScenePage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<ScenePage />} />
      <Route path="/debug" element={<DebugApp />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
