import React, { useState, useEffect } from "react"
import axios from "axios"

import './App.css';


function App() {
  const [scannerState, setScannerState] = useState({
    advance_toggle: {active: false, enabled: false},
    current_frame_index: 0,
    fast_forward_toggle: {active: false, enabled: false},
    is_scanning: false,
    is_scan_button_enabled: false,
    last_scan_end_info: "dismissed",
    light_toggle: {active: false, enabled: false},
    time_remaining: "-",
    zoom_toggle: {active: false, enabled: false},
  })
  const [nFrames, setNFrames] = useState(0)
  const [outputDirectory, setOutputDirectory] = useState("")
  const [showPanel, setShowPanel] = useState(true)

  useEffect(() => {
    axios.get("/backend/dashboard").then(response => setScannerState(response.data))
    axios.get("backend/scan-setup").then(response => {
      setNFrames(response.data.n_frames)
      setOutputDirectory(response.data.output_directory)
    })

    const sse = new EventSource("/backend/dashboard-sse")
    sse.addEventListener("state", e => setScannerState(JSON.parse(e.data)))
    sse.addEventListener("scan_setup", e => {
      const scanSetup = JSON.parse(e.data)
      setNFrames(scanSetup.n_frames)
      setOutputDirectory(scanSetup.output_directory)
    })
    sse.onerror = e => sse.close()

    return () => sse.close()
  }, [])

  const startScan = () => axios.post("/backend/scan", {output_directory: outputDirectory, n_frames: nFrames})

  return (
    <div className="grid grid-cols-1 sm:flex w-screen h-screen">
      <Preview
        scannerState={scannerState}
        nFrames={nFrames}
        startScan={startScan}
        showPanel={showPanel}
        setShowPanel={setShowPanel}
      />
      <Controls
        scannerState={scannerState}
        nFrames={nFrames}
        setNFrames={setNFrames}
        outputDirectory={outputDirectory}
        setOutputDirectory={setOutputDirectory}
        showPanel={showPanel}
      />
    </div>
  )
}


const Preview = ({ scannerState, nFrames, startScan, showPanel, setShowPanel }) => {

  const toggleZoom = () => axios.post("/backend/focuszoom")
  const toggleLight = () => axios.post("/backend/light")

  return (
    <div className="grow shrink justify-center flex bg-black relative">
      <img className="w-screen sm:w-auto sm:h-screen object-contain" src="/backend/preview" alt="Preview of the current frame"></img>

      {/* Scan progress overlay - top */}
      {scannerState.is_scanning && (
        <div className="absolute top-0 left-0 right-0 bg-black/80 px-4 py-3">
          <div className="text-white text-sm font-mono text-center tracking-wide">
            {scannerState.current_frame_index} / {nFrames} | {scannerState.time_remaining}
          </div>
          <div className="w-full bg-white/20 rounded-full h-1.5 mt-2">
            <div
              className="bg-red-500 h-1.5 rounded-full transition-all duration-300"
              style={{width: `${(scannerState.current_frame_index / nFrames) * 100}%`}}
            ></div>
          </div>
        </div>
      )}

      {/* Top right controls - Light, Zoom, Settings */}
      <div className="absolute top-4 right-4 flex gap-2">
        {/* Light button */}
        <button
          onClick={toggleLight}
          disabled={!scannerState.light_toggle.enabled}
          className={`${scannerState.light_toggle.active ? 'text-yellow-400 bg-yellow-400/20' : 'text-white bg-black/40'} hover:bg-black/60 disabled:text-gray-600 disabled:bg-black/20 disabled:cursor-not-allowed transition-all rounded-full p-2.5`}
          title={scannerState.light_toggle.active ? "Turn off light" : "Turn on light"}
        >
          <svg className="w-5 h-5 sm:w-6 sm:h-6" fill={scannerState.light_toggle.active ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z"/>
          </svg>
        </button>

        {/* Zoom button */}
        <button
          onClick={toggleZoom}
          disabled={!scannerState.zoom_toggle.enabled}
          className={`${scannerState.zoom_toggle.active ? 'text-blue-400 bg-blue-400/20' : 'text-white bg-black/40'} hover:bg-black/60 disabled:text-gray-600 disabled:bg-black/20 disabled:cursor-not-allowed transition-all rounded-full p-2.5`}
          title={scannerState.zoom_toggle.active ? "Zoom out" : "Zoom in"}
        >
          <svg className="w-5 h-5 sm:w-6 sm:h-6" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            {scannerState.zoom_toggle.active ? (
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607zM13.5 10.5h-6"/>
            ) : (
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607zM10.5 7.5v6m3-3h-6"/>
            )}
          </svg>
        </button>

        {/* Toggle panel button */}
        <button
          onClick={() => setShowPanel(!showPanel)}
          className={`${showPanel ? 'text-blue-400 bg-blue-400/20' : 'text-white bg-black/40'} hover:bg-black/60 transition-all rounded-full p-2.5`}
          title={showPanel ? "Hide settings" : "Show settings"}
        >
          <svg className="w-5 h-5 sm:w-6 sm:h-6" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.212-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/>
          </svg>
        </button>
      </div>

      {/* Bottom Controls Overlay */}
      <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/90 via-black/50 to-transparent p-4 pb-8">
        <div className="flex justify-center items-center gap-3 sm:gap-5">
          {/* Rewind */}
          <button
            onClick={() => axios.post("/backend/rewind")}
            disabled={!scannerState.fast_forward_toggle.enabled}
            className="text-white/80 hover:text-white disabled:text-white/30 disabled:cursor-not-allowed transition-colors"
            title="Rewind"
          >
            <svg className="w-7 h-7 sm:w-9 sm:h-9" fill="currentColor" viewBox="0 0 24 24">
              <path d="M11 18V6l-8.5 6 8.5 6zm.5-6l8.5 6V6l-8.5 6z"/>
            </svg>
          </button>

          {/* -1 Frame */}
          <button
            onClick={() => axios.post("/backend/rewind")}
            disabled={!scannerState.advance_toggle.enabled}
            className="text-white/80 hover:text-white disabled:text-white/30 disabled:cursor-not-allowed transition-colors"
            title="Previous frame"
          >
            <svg className="w-6 h-6 sm:w-7 sm:h-7" fill="currentColor" viewBox="0 0 24 24">
              <path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>
            </svg>
          </button>

          {/* Stop/Pause button - only visible when fast forwarding */}
          {scannerState.fast_forward_toggle.active && (
            <button
              onClick={() => axios.post("/backend/stop")}
              className="w-10 h-10 sm:w-12 sm:h-12 rounded-full bg-white/20 hover:bg-white/30 flex items-center justify-center transition-all"
              title="Stop"
            >
              <svg className="w-5 h-5 sm:w-6 sm:h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
                <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z"/>
              </svg>
            </button>
          )}

          {/* Record/Stop button - iOS camera style */}
          <button
            onClick={startScan}
            disabled={!scannerState.is_scan_button_enabled}
            className="relative w-16 h-16 sm:w-20 sm:h-20 rounded-full border-[3px] border-white/90 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed transition-all hover:scale-105 active:scale-95"
            title={scannerState.is_scanning ? "Stop scan" : "Start scan"}
          >
            <div className={`transition-all duration-200 ease-out ${
              scannerState.is_scanning
                ? 'w-5 h-5 sm:w-6 sm:h-6 bg-red-500 rounded-[4px]'
                : 'w-11 h-11 sm:w-14 sm:h-14 bg-red-500 rounded-full'
            }`}></div>
          </button>

          {/* +1 Frame */}
          <button
            onClick={() => axios.post("/backend/advance")}
            disabled={!scannerState.advance_toggle.enabled}
            className="text-white/80 hover:text-white disabled:text-white/30 disabled:cursor-not-allowed transition-colors"
            title="Next frame"
          >
            <svg className="w-6 h-6 sm:w-7 sm:h-7" fill="currentColor" viewBox="0 0 24 24">
              <path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
            </svg>
          </button>

          {/* Fast-Forward */}
          <button
            onClick={() => axios.post("/backend/fastforward")}
            disabled={!scannerState.fast_forward_toggle.enabled}
            className="text-white/80 hover:text-white disabled:text-white/30 disabled:cursor-not-allowed transition-colors"
            title="Fast-forward"
          >
            <svg className="w-7 h-7 sm:w-9 sm:h-9" fill="currentColor" viewBox="0 0 24 24">
              <path d="M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  )
}


const Controls = ({ scannerState, nFrames, setNFrames, outputDirectory, setOutputDirectory, showPanel }) => {

  const [isEndOfUse, setIsEndOfUse] = useState(false)
  const [isShowingPoweroffModal, setIsShowingPoweroffModal] = useState(false)
  const [expandedSections, setExpandedSections] = useState({
    camera: true,
    reel: false,
    output: false
  })

  const [cameraSettings, setCameraSettings] = useState(null)

  useEffect(() => {
    axios.get("/backend/camera-settings").then(response => {
      setCameraSettings(response.data)
      // Force apply settings after a delay to ensure camera is started
      setTimeout(() => {
        axios.post("/backend/camera-settings", response.data)
      }, 2000)
    })
  }, [])

  const poweroff = () => {
    axios.post("/backend/poweroff")
    setIsShowingPoweroffModal(false)
    setIsEndOfUse(true)
  }

  const openPoweroffModal = () => setIsShowingPoweroffModal(true)
  const closePoweroffModal = () => setIsShowingPoweroffModal(false)

  const onOutputDirectoryChange = event => setOutputDirectory(event.target.value)
  const onNFramesChange = event => setNFrames(event.target.value)

  const updateCameraSetting = (key, value) => {
    const newSettings = {...cameraSettings, [key]: value}
    setCameraSettings(newSettings)
    axios.post("/backend/camera-settings", newSettings)
  }

  const resetCameraToDefaults = () => {
    const defaults = {
      shutter_speed: 10000,
      analog_gain: 1.0,
      digital_gain: 1.0,
      brightness: 0.0,
      contrast: 1.0,
      saturation: 1.0,
      sharpness: 1.0,
      awb_mode: "auto",
      flip_h: false,
      flip_v: false
    }
    setCameraSettings(defaults)
    axios.post("/backend/camera-settings", defaults)
  }

  const toggleFlipH = () => updateCameraSetting('flip_h', !cameraSettings.flip_h)
  const toggleFlipV = () => updateCameraSetting('flip_v', !cameraSettings.flip_v)

  if (!showPanel) {
    return null
  }

  const Section = ({ id, title, icon, children, action }) => (
    <div className="bg-gray-800 rounded-lg overflow-hidden">
      <button
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-750 transition-colors"
        onClick={() => setExpandedSections(prev => ({...prev, [id]: !prev[id]}))}
      >
        <div className="flex items-center gap-2">
          <span className="text-gray-400">{icon}</span>
          <span className="text-sm text-gray-200">{title}</span>
        </div>
        <div className="flex items-center gap-2">
          {action}
          <svg className={`w-3.5 h-3.5 text-gray-500 transition-transform duration-200 ${expandedSections[id] ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5"/>
          </svg>
        </div>
      </button>
      <div className={`transition-all duration-200 ease-out ${expandedSections[id] ? 'max-h-[600px] opacity-100' : 'max-h-0 opacity-0 overflow-hidden'}`}>
        <div className="px-3 pb-3 pt-1 space-y-2">
          {children}
        </div>
      </div>
    </div>
  )


  return (
    <div className="w-full sm:w-72 shrink-0 bg-gray-900 flex flex-col h-full">
      {/* Header */}
      <div className="px-3 py-2 border-b border-gray-800">
        <h1 className="text-sm font-medium text-gray-200">Scanner Settings</h1>
      </div>

      {/* Alerts */}
      <ScanSuccessAlert show={scannerState.last_scan_end_info === "success"} />
      <ScanFailureAlert show={scannerState.last_scan_end_info === "failure"} />

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">

        {/* Camera Section */}
        <Section
          id="camera"
          title="Camera"
          icon={<svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z"/><path strokeLinecap="round" strokeLinejoin="round" d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM18.75 10.5h.008v.008h-.008V10.5Z"/></svg>}
          action={
            <button
              onClick={(e) => { e.stopPropagation(); resetCameraToDefaults(); }}
              className="text-[10px] px-1.5 py-0.5 bg-gray-700 hover:bg-gray-600 text-gray-400 rounded transition-colors"
              disabled={scannerState.is_scanning}
              title="Reset to defaults"
            >
              Reset
            </button>
          }
        >
          {cameraSettings ? (
            <>
              <Slider label="Exposure" value={cameraSettings.shutter_speed} min={100} max={100000} step={100}
                onChange={v => updateCameraSetting('shutter_speed', v)}
                format={v => `${(v/1000).toFixed(1)}ms`}
                disabled={scannerState.is_scanning} />

              <Slider label="ISO Gain" value={cameraSettings.analog_gain} min={1} max={16} step={0.1}
                onChange={v => updateCameraSetting('analog_gain', v)}
                format={v => `${v.toFixed(1)}x`}
                disabled={scannerState.is_scanning} />

              <Slider label="Brightness" value={cameraSettings.brightness} min={-1} max={1} step={0.01}
                onChange={v => updateCameraSetting('brightness', v)}
                format={v => v.toFixed(2)}
                disabled={scannerState.is_scanning} />

              <Slider label="Contrast" value={cameraSettings.contrast} min={0} max={32} step={0.1}
                onChange={v => updateCameraSetting('contrast', v)}
                format={v => v.toFixed(1)}
                disabled={scannerState.is_scanning} />

              <Slider label="Saturation" value={cameraSettings.saturation} min={0} max={32} step={0.1}
                onChange={v => updateCameraSetting('saturation', v)}
                format={v => v.toFixed(1)}
                disabled={scannerState.is_scanning} />

              <Slider label="Sharpness" value={cameraSettings.sharpness} min={0} max={16} step={0.1}
                onChange={v => updateCameraSetting('sharpness', v)}
                format={v => v.toFixed(1)}
                disabled={scannerState.is_scanning} />

              <div>
                <span className="text-xs text-gray-400 block mb-1">White Balance</span>
                <select
                  value={cameraSettings.awb_mode}
                  onChange={e => updateCameraSetting('awb_mode', e.target.value)}
                  className="w-full bg-gray-700 border border-gray-600 text-gray-200 text-xs rounded px-2 py-1.5 outline-none focus:border-gray-500"
                  disabled={scannerState.is_scanning}
                >
                  <option value="auto">Auto</option>
                  <option value="off">Off (Manual)</option>
                  <option value="tungsten">Tungsten</option>
                  <option value="fluorescent">Fluorescent</option>
                  <option value="indoor">Indoor</option>
                  <option value="daylight">Daylight</option>
                  <option value="cloudy">Cloudy</option>
                </select>
              </div>

              <div>
                <span className="text-xs text-gray-400 block mb-1">Orientation</span>
                <div className="flex gap-1.5">
                  <button
                    onClick={toggleFlipH}
                    className={`flex-1 py-1.5 text-xs rounded transition-all ${
                      cameraSettings.flip_h
                        ? 'bg-gray-600 text-white'
                        : 'bg-gray-700 text-gray-400 hover:bg-gray-650'
                    }`}
                    disabled={scannerState.is_scanning}
                    title="Flip horizontally"
                  >
                    Flip H
                  </button>
                  <button
                    onClick={toggleFlipV}
                    className={`flex-1 py-1.5 text-xs rounded transition-all ${
                      cameraSettings.flip_v
                        ? 'bg-gray-600 text-white'
                        : 'bg-gray-700 text-gray-400 hover:bg-gray-650'
                    }`}
                    disabled={scannerState.is_scanning}
                    title="Flip vertically"
                  >
                    Flip V
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="text-xs text-gray-500 py-2">Loading...</div>
          )}
        </Section>

        {/* Reel Section */}
        <Section
          id="reel"
          title="Reel Size"
          icon={<svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg>}
        >
          <select
            onChange={e => e.target.value !== "custom" && setNFrames(parseInt(e.target.value))}
            className="w-full bg-gray-700 border border-gray-600 text-gray-200 text-xs rounded px-2 py-1.5 outline-none focus:border-gray-500"
            disabled={scannerState.is_scanning}
          >
            <optgroup label="Regular 8mm (16 fps)">
              <option value="4000">3" / 50ft (~4000 frames)</option>
              <option value="16000">5" / 200ft (~16000 frames)</option>
              <option value="24000">6" / 300ft (~24000 frames)</option>
              <option value="32000">7" / 400ft (~32000 frames)</option>
            </optgroup>
            <optgroup label="Super 8 (18 fps)">
              <option value="3600">3" / 50ft (~3600 frames)</option>
              <option value="14400">5" / 200ft (~14400 frames)</option>
              <option value="21600">6" / 300ft (~21600 frames)</option>
              <option value="28800">7" / 400ft (~28800 frames)</option>
            </optgroup>
            <optgroup label="Other">
              <option value="custom">Custom...</option>
            </optgroup>
          </select>
          <div>
            <span className="text-xs text-gray-400 block mb-1">Frame Count</span>
            <input
              type="number"
              value={nFrames}
              onChange={onNFramesChange}
              className="w-full bg-gray-700 border border-gray-600 text-gray-200 text-xs rounded px-2 py-1.5 outline-none focus:border-gray-500 font-mono"
              disabled={scannerState.is_scanning}
            />
          </div>
        </Section>

        {/* Output Section */}
        <Section
          id="output"
          title="Output"
          icon={<svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 0 1 4.5 9.75h15A2.25 2.25 0 0 1 21.75 12v.75m-8.69-6.44-2.12-2.12a1.5 1.5 0 0 0-1.061-.44H4.5A2.25 2.25 0 0 0 2.25 6v12a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9a2.25 2.25 0 0 0-2.25-2.25h-5.379a1.5 1.5 0 0 1-1.06-.44Z"/></svg>}
        >
          <div>
            <span className="text-xs text-gray-400 block mb-1">Directory</span>
            <input
              type="text"
              value={outputDirectory}
              onChange={onOutputDirectoryChange}
              placeholder="/path/to/output"
              className="w-full bg-gray-700 border border-gray-600 text-gray-200 text-xs rounded px-2 py-1.5 outline-none focus:border-gray-500 font-mono"
              disabled={scannerState.is_scanning}
            />
          </div>
        </Section>
      </div>

      {/* Footer */}
      <div className="p-2 border-t border-gray-800">
        <button
          onClick={openPoweroffModal}
          disabled={scannerState.is_scanning}
          className="w-full py-1.5 text-xs text-gray-500 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Power off
        </button>
      </div>

      <PoweroffModal onConfirm={poweroff} onAbort={closePoweroffModal} show={isShowingPoweroffModal} />
      <EndOfUseModal show={isEndOfUse} />
    </div>
  )
}


const Slider = ({ label, value, min, max, step, onChange, format, disabled }) => {
  const [localValue, setLocalValue] = useState(value)
  const [isDragging, setIsDragging] = useState(false)

  // Sync with external value when not dragging
  useEffect(() => {
    if (!isDragging) {
      setLocalValue(value)
    }
  }, [value, isDragging])

  const handleChange = (e) => {
    setLocalValue(parseFloat(e.target.value))
  }

  const handleEnd = () => {
    setIsDragging(false)
    onChange(localValue)
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs text-gray-400">{label}</span>
        <span className="text-xs font-mono text-gray-500">{format(localValue)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={localValue}
        onMouseDown={() => setIsDragging(true)}
        onTouchStart={() => setIsDragging(true)}
        onChange={handleChange}
        onMouseUp={handleEnd}
        onTouchEnd={handleEnd}
        className="w-full h-2 bg-gray-700 rounded-full cursor-pointer accent-gray-400"
        disabled={disabled}
      />
    </div>
  )
}


const ScanSuccessAlert = ({show}) => {
  const dismiss = () => axios.post("/backend/dismiss")

  if (!show) return null

  return (
    <div className="mx-2 mt-2 p-2 bg-green-900/30 border border-green-800 rounded flex items-center gap-2">
      <svg className="w-4 h-4 text-green-400 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5"/>
      </svg>
      <span className="text-xs text-green-300 flex-1">Scan completed</span>
      <button onClick={dismiss} className="text-green-500 hover:text-green-300">
        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
  )
}

const ScanFailureAlert = ({show}) => {
  const dismiss = () => axios.post("/backend/dismiss")

  if (!show) return null

  return (
    <div className="mx-2 mt-2 p-2 bg-red-900/30 border border-red-800 rounded flex items-center gap-2">
      <svg className="w-4 h-4 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
      </svg>
      <span className="text-xs text-red-300 flex-1">Scan failed</span>
      <button onClick={dismiss} className="text-red-500 hover:text-red-300">
        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
  )
}

const PoweroffModal = ({onConfirm, onAbort, show}) => {
  if (!show) return null

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-lg shadow-xl max-w-xs w-full p-4">
        <h3 className="text-sm font-medium text-white mb-2">Power Off?</h3>
        <p className="text-xs text-gray-400 mb-4">The scanner will shut down.</p>
        <div className="flex gap-2">
          <button onClick={onAbort} className="flex-1 py-1.5 text-xs text-gray-300 bg-gray-700 hover:bg-gray-600 rounded transition-colors">Cancel</button>
          <button onClick={onConfirm} className="flex-1 py-1.5 text-xs text-white bg-red-600 hover:bg-red-500 rounded transition-colors">Power Off</button>
        </div>
      </div>
    </div>
  )
}

const EndOfUseModal = ({show}) => {
  if (!show) return null

  return (
    <div className="fixed inset-0 bg-black flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-lg shadow-xl max-w-xs w-full p-4 text-center">
        <svg className="w-8 h-8 text-gray-500 mx-auto mb-2" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M5.636 5.636a9 9 0 1 0 12.728 0M12 3v9"/>
        </svg>
        <h3 className="text-sm font-medium text-white mb-1">Powering Off...</h3>
        <p className="text-xs text-gray-400">You can disconnect power soon.</p>
      </div>
    </div>
  )
}

export default App;
