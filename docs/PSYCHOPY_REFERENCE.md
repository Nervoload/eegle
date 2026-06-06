PsychoPy API Reference Summary
This document summarises key modules in the PsychoPy API (v2026.1.3). It provides a high‑level overview of each package, including its purpose, common classes/functions and illustrative code fragments. The intent is to replicate the reference documentation locally for convenient use when designing tasks such as a psychomotor vigilance task (PVT) in a closed‑loop neurofeedback system. Each section cites the official documentation.
1. psychopy.core – basic functions
The core module provides low‑level utilities such as timing and program flow control. It includes a family of clock classes for measuring time and a wait function to pause execution.
Clock classes. Clock is a resettable timer; you can create multiple clocks to measure different intervals. CountdownTimer counts down from a start value; it is resettable and returns negative times when it overruns. MonotonicClock measures sub‑millisecond time from its creation and cannot be reset. StaticPeriod manages a fixed inter‑stimulus interval while allowing code to run; typical usage is shown below.
from psychopy import core
# create a 0.5‑s static period on a 60 Hz monitor
ISI = core.StaticPeriod(screenHz=60)
ISI.start(0.5)
# run code while the timer is running, e.g. load an image
stim.image = 'largeFile.bmp'
ISI.complete()  # wait out the remaining time
Functions. wait(secs, hogCPUperiod=0.2) halts execution for secs seconds, using a combination of time.sleep and busy waiting for accuracy. getAbsTime() returns Unix time, and getTime(applyZero=True) returns time since module import.
2. psychopy.clock – clocks and timers
This module provides the same clock classes as psychopy.core (for backwards compatibility) but separated into a dedicated package. It defines a high‑resolution timebase used across PsychoPy and avoids duplicating clock logic in other modules. The Clock, CountdownTimer, MonotonicClock and StaticPeriod classes behave identically to those in psychopy.core.
3. psychopy.session – running sessions with multiple experiments
Session encapsulates running multiple experiments within a single PsychoPy session. It manages a persistent window and input devices so experiments don’t need to repeatedly open and close windows. Sessions can be controlled from secondary threads, allowing tasks to be paused or modified while running. Experiments are loaded from .psyexp files located under a given root directory and can be added to the session with addExperiment().
The class includes methods such as addData(name, value) to log data, addAnnotation() to annotate the data file and log, and pauseExperiment() to pause execution. Multi‑threading is supported by calling methods with blocking=False; these calls queue actions for execution on the main thread.
4. psychopy.visual – visual stimuli
The visual package contains classes and functions for presenting visual stimuli. Experiments create a Window to display all stimuli. Important categories include:
Windows and devices. Window is the main class for drawing; other classes support warped displays (Warper), 3‑D headsets (Rift) and projector frame packing.
Common stimuli. ImageStim displays images; TextStim displays text; TextBox2 is a more advanced editable text box; ShapeStim and its subclasses draw basic shapes (rectangles, circles, polygons, lines, pies). GratingStim, RadialStim and NoiseStim produce patterned stimuli.
Multiple stimuli and arrays. ElementArrayStim displays many stimuli simultaneously; DotStim presents moving dots for motion perception experiments.
3D stimuli. Classes such as LightSource, SceneSkybox, BlinnPhongMaterial, SphereStim and BoxStim create 3‑D scenes.
Other stimuli. MovieStim plays movies; Slider and RatingScale collect continuous or categorical ratings. BufferImageStim and Aperture operate on other stimuli, e.g., taking screenshots or restricting visibility.
Helper modules provide colour‑space conversions, coordinate conversions, unit conversions and view/projection tools.
5. psychopy.hardware – hardware interfaces
PsychoPy can communicate with various external devices. The hardware package organises interfaces for keyboards, response boxes, cameras, EEG systems (BrainProducts), eye trackers, pumps and more. Each subpackage provides device‑specific classes. A convenience function findPhotometer(ports=None, device=None) sweeps serial ports to detect a connected photometer and returns a device object if found.
6. psychopy.iohub – event monitoring framework
iohub runs in a separate process to monitor devices such as keyboards, mice and eye trackers without blocking the experiment loop. Events are timestamped using the global PsychoPy clock and delivered to the PsychoPy script as they occur. Example scripts are available in psychopy/demos/coder/iohub. Events may be saved in HDF5 format for later analysis.
7. psychopy.tools – miscellaneous tools
This module aggregates miscellaneous functions and classes. Subpackages cover colour conversions (colorspacetools), coordinate conversions (coordinatetools), file utilities (filetools), OpenGL helpers (gltools), image handling (imagetools), mathematical operations (mathtools), monitor unit conversions (monitorunittools), movie handling (movietools), package information (pkgtools), plotting (plottools), Rift/VR support (rifttools), system utilities (systemtools), type conversions (typetools) and unit conversions (unittools). Use these modules directly to avoid clutter in psychopy.misc.
8. psychopy.app – PsychoPy application suite
The app module provides functions for starting and controlling the PsychoPy GUI (Coder/Builder/Runner). startApp() launches the application; quitApp() closes it; isAppStarted() checks whether the GUI is running. Functions getAppInstance() and getAppFrame(frameName) return the application instance or specific GUI frames (coder/builder/runner). These functions are mainly for testing or extending the GUI.
9. psychopy.colors – working with colours
This module defines a Color class that stores colour values in a specified colour space and can convert between spaces. It supports properties such as .rgb, .hsv, .dkl, .lms and their variants (with alpha). The method getReadable(contrast) computes a contrasting colour for text. Deprecated utility functions include isValidColor() and hex2rgb255().
10. psychopy.data – data storage and analysis
psychopy.data offers classes to manage experimental data. Important handlers include:
ExperimentHandler. Container for managing multiple loops or staircases; it records trials into a single data file and handles saving. It stores metadata (experiment name, version, participant info) and provides methods to add data and annotations.
TrialHandler & friends. TrialHandler defines a sequence of trials; TrialHandler2 allows mid‑run updates; TrialHandlerExt supports oddball designs. StairHandler, QuestHandler, QuestPlusHandler, PsiHandler and MultiStairHandler implement adaptive staircase and Bayesian threshold algorithms.
Utility functions include importConditions() to load trial conditions from CSV/Excel, functionFromStaircase() to turn a staircase into a psychometric function, bootStraps() to generate bootstrap resamples and getDateStr() to create date‑stamped filenames. Curve‑fitting classes such as FitWeibull and FitLogistic assist in data analysis.
11. psychopy.event – keypresses and mouse clicks
event contains classes to capture input devices during experiments. The Mouse class tracks mouse position and button presses; it can reset click timing, return absolute or relative positions and detect clicks within shapes. Methods include getPos(), getPressed(getTime=False), getRel(), getWheelRel(), mouseMoved(), setPos() and setVisible(). The module also provides functions to fetch keyboard events (getKeys), but those are documented in the full API.
12. psychopy.filters – creating filters
This module (now part of psychopy.visual.filters) contains functions to create 2‑D Butterworth filters and other textures. For instance, butter2d_bp(size, cutin, cutoff, n) creates a band‑pass filter; butter2d_hp and butter2d_lp make high‑pass and low‑pass filters. There are also functions to convolve matrices (conv2d), compute root‑mean‑square contrast (getRMScontrast), perform 2‑D FFTs (imfft/imifft) and generate gratings (make2DGauss, makeGrating)..
13. psychopy.gui – dialog boxes
The gui module lets experiments query participants via simple dialogs. DlgFromDict builds a dialog from a dictionary; keys define input fields and values provide defaults. It returns True/False depending on whether the user clicked OK and writes updated values back to the dictionary. Dlg provides a lower‑level dialog builder; you can add text or fields sequentially and call show() to present it.
14. psychopy.info – system information
info gathers system and runtime metadata. The RunTimeInfo class captures configuration details of PsychoPy, the experiment script, the computer, the Python environment and OpenGL settings. It returns a dictionary summarising parameters such as PsychoPy version, script author/version, system hostname, monitor details, Python package versions and OpenGL extensions. Utility functions measure memory usage (getMemoryUsage()) and RAM (getRAM()).
15. psychopy.layout – vectors and points
This package defines classes to represent vectors (Vector), positions (Position), sizes (Size) and vertex arrays (Vertices). These objects support unit conversions between pixels, degrees of visual angle, centimeters, normalized coordinates, etc.. Vectors can be compared using operators and converted to different units via properties like .cm, .deg, .norm and .pix. The module aids in positioning stimuli on the screen and computing distances in different coordinate systems.
16. psychopy.logging – logging control
PsychoPy uses Python’s logging module to record messages. logging defines log levels (DEBUG, INFO, EXP, DATA, WARNING, ERROR, CRITICAL) and allows sending messages to multiple targets. The LogFile class writes logs to a file or stream with a specified minimum level. The _Logger class manages log targets and formats messages. Convenience functions such as logging.data(), logging.exp(), logging.debug() and logging.error() send messages at particular levels.
17. psychopy.misc – miscellaneous routines
Historically a catch‑all, misc now re‑exports functions from various psychopy.tools modules. It includes file utilities (toFile(), fromFile(), mergeFolder()), colour conversions (dkl2rgb(), hsv2rgb(), lms2rgb()), coordinate conversions (cart2pol(), pol2cart(), sph2cart()), monitor unit conversions (convertToPix(), cm2deg(), deg2pix()), image utilities (array2image(), image2array()), plotting (plotFrameIntervals()) and numeric conversions (float_uint8(), uint8_float()). Each function is documented in its original submodule.
18. psychopy.monitors – monitor calibration
This package manages monitor calibration outside the GUI. The Monitor class stores calibration details (width, viewing distance, gamma) and loads saved profiles. You can override parameters at instantiation (e.g., distance=114). Methods allow saving and deleting calibrations, computing colour‑conversion matrices (getDKL_RGB(), getLMS_RGB()), retrieving luminance values (getLumsPre(), getMeanLum()), and getting screen size in pixels (getSizePix()).
19. psychopy.preferences – user preferences
preferences lets scripts override user settings on a per‑experiment basis. For example, to select the audio backend you can set prefs.hardware['audioLib'] before importing psychopy.sound. The Preferences class loads and saves preferences files; helper methods such as getPaths(), loadAll(), saveUserPrefs() and validate() manage configuration files.
20. psychopy.web – web utilities
The web module tests and configures internet connectivity. haveInternetAccess() returns a boolean indicating connectivity. requireInternetAccess() raises an error if no connection is available. setupProxy() attempts to configure proxy settings by examining system proxy configurations and PsychoPy preferences.
Notes for Closed‑Loop PVT Task
When implementing a psychomotor vigilance task as part of a closed‑loop neurofeedback pipeline, the following components are particularly relevant:
Timing: Use core.Clock or core.MonotonicClock for accurate reaction‑time measurement and core.StaticPeriod to enforce inter‑trial intervals.
Visual stimuli: Use visual.Window to create the task display and visual.TextStim or visual.ShapeStim to present the stimulus and fixation.
Input: Capture responses with event.Mouse or event.getKeys() and log reaction times using the clock.
Data: Store participant responses and timings with data.ExperimentHandler or TrialHandler.
Logging: Use logging to record events and debug messages.
This reference summarises the API but does not replace the full PsychoPy documentation, which should be consulted for advanced usage and all method signatures.
