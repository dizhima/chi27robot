import "./secureContextPolyfill";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./style.css";
import { sceneLabelScale } from "./sceneLabelScale";
import { uiTheme } from "./theme";

document.documentElement.dataset.uiTheme = uiTheme;
document.documentElement.style.setProperty(
  "--scene-label-scale",
  String(sceneLabelScale),
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
