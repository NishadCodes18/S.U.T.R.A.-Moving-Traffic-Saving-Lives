const POLL_MS = 300;
const MAX_LOG_LINES = 25;

const leftLight = document.getElementById("trafficLightLeft");
const rightLight = document.getElementById("trafficLightRight");
const safetyText = document.getElementById("safetyText");
const roadText = document.getElementById("roadText");
const v2iText = document.getElementById("v2iText");

const safetyCard = document.getElementById("safetyCard");
const roadCard = document.getElementById("roadCard");
const v2iCard = document.getElementById("v2iCard");

const ambulanceCard = document.getElementById("ambulanceCard");
const festivalCard = document.getElementById("festivalCard");
const ambConfidenceText = document.getElementById("ambConfidence");
const audioStatusText = document.getElementById("audioStatusText");
const visibleAudioPlayer = document.getElementById("visibleAudioPlayer");

const trafficCountA = document.getElementById("trafficCountA");
const trafficCountB = document.getElementById("trafficCountB");
const countdownTimer = document.getElementById("countdownTimer");
const logBox = document.getElementById("logBox");
const uptimeDisplay = document.getElementById("uptime");

const festivalToggle = document.getElementById("festivalToggle");
const multimodalToggle = document.getElementById("multimodalToggle");

const commandInput = document.getElementById("commandInput");
const commandRun = document.getElementById("commandRun");
const commandOutput = document.getElementById("commandOutput");
const eventsBox = document.getElementById("eventsBox");
const eventsRefresh = document.getElementById("eventsRefresh");
const liveBadge = document.getElementById("liveBadge");
const liveFeed = document.getElementById("liveFeed");
const statusBadges = document.getElementById("statusBadges");
const videoLoading = document.getElementById("videoLoading");

let lastLog = "";

// 🚨 BROWSER MEMORY (Persistence on Refresh)
window.addEventListener("DOMContentLoaded", () => {
    const lastCmd = localStorage.getItem("lastSutraCmd");
    if (lastCmd) {
        runCommand(lastCmd, true);
    }
});

if (liveFeed) {
    liveFeed.onload = function() {
        if(videoLoading) videoLoading.classList.add("hidden");
    };
}

if (visibleAudioPlayer) {
    visibleAudioPlayer.addEventListener("play", () => {
        fetch("/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cmd: "/siren on" }) });
        window.sirenInterval = setInterval(() => {
            fetch("/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cmd: "/siren on" }) });
        }, 3000);
    });
    
    visibleAudioPlayer.addEventListener("pause", () => {
        clearInterval(window.sirenInterval);
    });
}

let uptimeSeconds = 0;
setInterval(() => {
  uptimeSeconds++;
  const h = Math.floor(uptimeSeconds / 3600).toString().padStart(2, "0");
  const m = Math.floor((uptimeSeconds % 3600) / 60).toString().padStart(2, "0");
  const s = (uptimeSeconds % 60).toString().padStart(2, "0");
  if (uptimeDisplay) uptimeDisplay.textContent = `${h}:${m}:${s}`;
}, 1000);

function updateTrafficLight(lightContainer, activeColor) {
  if (!lightContainer) return;
  const lights = document.getElementById(lightContainer).querySelectorAll(".light");
  lights.forEach((l) => l.classList.remove("active"));
  const active = document.getElementById(lightContainer).querySelector(`.light[data-color="${activeColor}"]`);
  if (active) active.classList.add("active");
}

function appendLog(line) {
  if (!line || line === lastLog) return;
  lastLog = line;
  const now = new Date().toLocaleTimeString("en-US", { hour12: false });
  const entry = document.createElement("div");

  if (line.includes("EMERGENCY") || line.includes("SOS") || line.includes("MULTIMODAL") || line.includes("AMBULANCE")) {
    entry.style.color = "var(--danger)";
  } else if (line.includes("Festival") || line.includes("Siren")) {
    entry.style.color = "var(--success)";
  } else if (line.includes("Awaiting") || line.includes("warning") || line.includes("Animal") || line.includes("DISPATCHED")) {
    entry.style.color = "var(--warning)";
  } else {
    entry.style.color = "var(--text-secondary)";
  }

  entry.innerHTML = `> [${now}] ${line}`;
  logBox.appendChild(entry);
  while (logBox.children.length > MAX_LOG_LINES) logBox.removeChild(logBox.firstChild);
  logBox.scrollTop = logBox.scrollHeight;
}

function updateStatusBadges(data) {
  if (!statusBadges) return;
  statusBadges.innerHTML = "";
  const badges = [];
  if (data.current_mode === "IMAGE") badges.push({ c: "warn", t: "Static Image" });
  if (data.using_video_fallback) badges.push({ c: "warn", t: "Video Stream" });
  else if (data.feed_available && data.current_mode === "CAMERA") badges.push({ c: "ok", t: "Live Camera" });
  
  badges.forEach((b) => {
    const span = document.createElement("span");
    span.className = `status-badge ${b.c}`;
    span.textContent = b.t;
    statusBadges.appendChild(span);
  });
}

async function refreshEvents() {
  try {
    const filter = document.getElementById("eventFilter")?.value || "all";
    const url = filter === "all" ? "/events" : `/events?type=${encodeURIComponent(filter)}`;
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return;
    const events = await res.json();
    if (!eventsBox) return;
    eventsBox.innerHTML = events.slice(-20).reverse().map((e) =>
      `<div class="event"><span class="event-type">[${e.type.toUpperCase()}]</span>${e.message}</div>`
    ).join("") || "<div class='event'>No events</div>";
  } catch (e) {}
}

async function refreshStatus() {
  try {
    const res = await fetch("/status", { cache: "no-store" });
    const data = await res.json();

    updateTrafficLight("trafficLightLeft", data.traffic_light_a);
    updateTrafficLight("trafficLightRight", data.traffic_light_b);

    if (safetyText) safetyText.textContent = data.safety;
    if (roadText) roadText.textContent = data.road;
    if (v2iText) v2iText.textContent = data.v2i_status;
    if (trafficCountA) trafficCountA.textContent = data.traffic_count_a;
    if (trafficCountB) trafficCountB.textContent = data.traffic_count_b;
    
    if (audioStatusText) {
        if (data.siren_detected) audioStatusText.innerHTML = '<span style="color: var(--success);">Z-Score Met: Siren Detected</span>';
        else audioStatusText.innerHTML = 'Listening for Siren...';
    }
    
    if(ambConfidenceText) ambConfidenceText.textContent = data.amb_confidence.toFixed(1);
    
    if (countdownTimer) {
        countdownTimer.textContent = data.countdown === 99 ? "OVERRIDE" : data.countdown + "s";
    }

    if (festivalToggle) {
      festivalToggle.textContent = data.festival_mode ? "ON" : "OFF";
      festivalToggle.classList.toggle("active", !!data.festival_mode);
      
      // 🚨 FIX: Ensure the festival pop-up actually displays on the left menu!
      if(festivalCard) {
          festivalCard.style.display = data.festival_mode ? "flex" : "none";
      }
    }
    
    if (multimodalToggle) {
      multimodalToggle.textContent = data.strict_multimodal ? "ON" : "OFF";
      multimodalToggle.classList.toggle("active", !!data.strict_multimodal);
    }

    updateStatusBadges(data);

    const isAmb = data.green_corridor_active; 
    const sosActive = data.safety.includes("SOS") || data.safety.includes("HELP");

    if (ambulanceCard) {
      if (isAmb || (data.ai_log.includes("Awaiting audio") && data.current_mode === "IMAGE")) ambulanceCard.style.display = "flex";
      else ambulanceCard.style.display = "none";
    }

    const vidWrapper = document.getElementById("videoContainer");
    if(vidWrapper) {
        if(sosActive || isAmb) vidWrapper.classList.add("emergency-flash");
        else vidWrapper.classList.remove("emergency-flash");
    }

    if (liveBadge) liveBadge.classList.toggle("offline", !data.feed_available);

    safetyCard.classList.remove("safe", "alert", "warning");
    roadCard.classList.remove("safe", "alert", "warning");
    v2iCard.classList.remove("safe", "alert", "warning");

    if (sosActive) safetyCard.classList.add("alert"); else safetyCard.classList.add("safe");
    if (data.road.includes("DISPATCHED")) roadCard.classList.add("warning"); else roadCard.classList.add("safe");
    if (data.v2i_status.includes("EVP")) v2iCard.classList.add("alert"); else if (data.v2i_status.includes("FORCE")) v2iCard.classList.add("warning"); else v2iCard.classList.add("safe");

    appendLog(data.ai_log);
  } catch (error) {
    if (statusBadges) statusBadges.innerHTML = '<span class="status-badge err">SERVER DISCONNECTED</span>';
  }
}

// 🚨 ==========================================
// 🚨 INTERACTIVE DASHBOARD TOUR SEQUENCE
// 🚨 ==========================================
const tutorialSteps = [
    { 
        id: "videoContainer", 
        title: "Live Vision Feed", 
        text: "We have temporarily turned off the video for this demo. Usually, this panel runs advanced Edge-AI to visually detect vehicles, pedestrians, and ambulances in real-time." 
    },
    { 
        id: "cardVision", 
        title: "Lane A Dynamic Signal", 
        text: "See this blinking red/green panel? This light dynamically extends its green time if traffic is heavy. If the camera spots an ambulance, it immediately flashes GREEN to clear the intersection." 
    },
    { 
        id: "cardAudio", 
        title: "Acoustic Siren Sensor", 
        text: "This audio panel constantly listens for specific siren frequencies using FFT math. If 'Strict Mode' is turned ON, the traffic light won't change unless this sensor actually hears the siren alongside the camera." 
    },
    { 
        id: "safetyCard", 
        title: "Guardian Angel (SOS)", 
        text: "This constantly watches for a distress hand gesture (Palm -> Tuck Thumb -> Make Fist). If detected, it triggers a global lockdown and dispatches police." 
    },
    { 
        id: "indiaModesCard", 
        title: "India-Specific Modes", 
        text: "Custom built for Indian road conditions. For example, turning on 'Festival Mode' extends crossing times to accommodate religious processions without breaking the flow." 
    }
];

let currentStep = 0;

function showTutorialStep() {
    // Clear old highlights
    document.querySelectorAll('.tutorial-highlight').forEach(el => {
        el.classList.remove('tutorial-highlight');
        el.style.pointerEvents = 'auto';
    });
    
    // Check if tutorial is over
    if (currentStep >= tutorialSteps.length) {
        document.getElementById('tutorialTooltip').style.display = 'none';
        document.getElementById('tutorialOverlay').style.display = 'none';
        document.getElementById('videoWrapper').style.opacity = '1'; // Turn video back on
        appendLog("=== DASHBOARD TOUR COMPLETE ===");
        return;
    }
    
    // Apply highlight to the targeted card
    const step = tutorialSteps[currentStep];
    const el = document.getElementById(step.id);
    if(el) {
        el.classList.add('tutorial-highlight');
        el.style.pointerEvents = 'none'; 
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    
    // Update tooltip text
    document.getElementById('tutorialTitle').textContent = step.title;
    document.getElementById('tutorialDesc').textContent = step.text;
    
    // Update button text on last step
    if(currentStep === tutorialSteps.length - 1) {
        document.getElementById('tutorialNextBtn').textContent = "Finish Tour";
    } else {
        document.getElementById('tutorialNextBtn').textContent = "Next Step";
    }
}

document.getElementById('tutorialNextBtn')?.addEventListener('click', () => {
    currentStep++;
    showTutorialStep();
});

function runInteractiveTutorial() {
    appendLog("=== LAUNCHING DASHBOARD TOUR ===");
    
    // Dim the screen, show tooltip, hide the video feed completely
    document.getElementById('tutorialOverlay').style.display = 'block';
    document.getElementById('tutorialTooltip').style.display = 'block';
    document.getElementById('videoWrapper').style.opacity = '0'; 
    
    currentStep = 0;
    showTutorialStep();
}
// ==========================================


async function runCommand(forcedCmd = null, isHidden = false) {
  const cmd = forcedCmd || commandInput?.value?.trim();
  if (!cmd) return;
  
  if(!isHidden) {
      const div = document.createElement('div');
      div.style.color = "#fff";
      div.style.marginBottom = "4px";
      div.textContent = `> ${cmd}`;
      commandOutput.appendChild(div);
      commandInput.value = "";
  }
  
  try {
    const res = await fetch("/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd }),
    });
    const data = await res.json();
    
    if (commandOutput && !isHidden) {
      const resp = document.createElement('div');
      resp.style.color = data.ok ? "var(--accent-secondary)" : "var(--warning)";
      resp.style.marginBottom = "8px";
      resp.style.whiteSpace = "pre-wrap";
      resp.textContent = data.output;
      commandOutput.appendChild(resp);
      commandOutput.scrollTop = commandOutput.scrollHeight;
    }

    // 🚨 If the backend says start_tutorial, trigger the JS!
    if (data.start_tutorial) {
        runInteractiveTutorial();
    }

    if (data.ok && cmd.startsWith("/use ") && !cmd.startsWith("/use audio")) {
        localStorage.setItem("lastSutraCmd", cmd);
    }
    
    if (data.play_audio && visibleAudioPlayer) {
        visibleAudioPlayer.src = "/static/sounds/" + data.play_audio;
        visibleAudioPlayer.load();
        visibleAudioPlayer.play(); 
    }
    
    if (data.ok && (cmd === "/use camera" || cmd === "/use video default" || (!isNaN(cmd) && !data.play_audio))) {
        if(videoLoading) videoLoading.classList.remove("hidden");
        if(liveFeed) liveFeed.src = "/video_feed?" + new Date().getTime(); 
    }
    
    if (data.ok && (cmd === "/events" || cmd === "/event")) refreshEvents();
  } catch (e) {
      if(!isHidden) {
          const err = document.createElement('div');
          err.style.color = "var(--danger)";
          err.textContent = "API Error: " + e.message;
          commandOutput.appendChild(err);
      }
  }
}

setInterval(refreshStatus, POLL_MS);
refreshStatus();
refreshEvents();

commandRun?.addEventListener("click", () => runCommand());
commandInput?.addEventListener("keydown", (e) => { if (e.key === "Enter") runCommand(); });
eventsRefresh?.addEventListener("click", refreshEvents);
document.getElementById("eventFilter")?.addEventListener("change", refreshEvents);

festivalToggle?.addEventListener("click", async () => {
  try {
    const res = await fetch("/festival_mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !festivalToggle.classList.contains("active") }) });
    if (res.ok) refreshStatus();
  } catch (e) {}
});

multimodalToggle?.addEventListener("click", async () => {
  try {
    const res = await fetch("/toggle_multimodal", { method: "POST", headers: { "Content-Type": "application/json" } });
    if (res.ok) refreshStatus();
  } catch (e) {}
});