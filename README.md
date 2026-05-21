# 🎯 FITS Image Player v1.0
### Native Astrophotography Streaming Engine for Siril

[FITS Image Player](https://github.com/user-attachments/assets/7e8bf176-7a26-441a-b383-c3562c17d697)

## 📝 Description
This application operates as a frame playback control panel featuring an integrated rendering engine designed to interface with Siril. It inherits Siril's current Working Directory (PWD) to dynamically locate `lights/` and `process/` folders, enabling the batch-processing of multiple target datasets provided that the PWD is set to the common root directory where the dataset subfolders reside; otherwise, it processes one dataset at a time. It applies asynchronous multi-core non-linear midtone stretching to render frames smoothly inside a dynamic canvas playback player.

---

## 🌟 Key Features
*   **Localized Relative Path Resolution:** Operates strictly within Siril's runtime path context without breaking folder structures.
*   **Granular Cache Persistence:** Smart frame-by-frame cache evaluation to process only newly added images or sets.
*   **Dynamic Histogram Rendering:** Real-time generation of source FITS linear histograms before stretching.
*   **Multi-core OpenCV Debayering:** High-performance, hardware-scaled debayer patterns for raw color matrices.
*   **Live HFR Telemetry Tracking:** Dynamic indexing and extraction of real-time star FWHM/HFR metrics parsed straight from Siril's `light_.seq`.
*   **Analytical Session Video Recording:** Export your streaming session directly into a WebM container using full-frame or focused Region of Interest (ROI) zoom cropping.

---

## 🛠️ Dependencies
The engine relies on the following core Python libraries:
*   `PyQt6` (GUI Architecture and Hardware Display Resolution Scaling)
*   `opencv-python` (High-performance Image Manipulation and WebM Video Recording)
*   `numpy` (Fast Matrix Operations and Percentile Calculations)
*   `astropy` (Atomic FITS Header and Data Matrix Extraction)

To install all dependencies at once, run:
```bash
pip install PyQt6 opencv-python numpy astropy
```

---

## 🚀 Usage Note
⚠️ **Important Execution Context:** This script is engineered to be executed exclusively within Siril's working environment or script directory, implicitly inheriting its runtime context, environment variables, and automated folder tree hierarchies.

### Keyboard Controls Quick Guide
While the dynamic canvas player is active, you can interact with the stream using the following bindings:
*   `[Space]` – Play / Pause the stream execution.
*   `[Left Arrow]` or `[<]` – Go to the Previous Frame.
*   `[Right Arrow]` or `[>]` – Go to the Next Frame.
*   `[Esc]`, `[Q]`, or `[X]` – Safely terminate and close the player instance.
<br>
<img width="900" height="506" alt="1" src="https://github.com/user-attachments/assets/3de1c316-934e-4799-a5d9-ec2ec2be9ce0" />
<br>
<br>
<img width="900" height="506" alt="2" src="https://github.com/user-attachments/assets/1648e4ba-732d-4a6a-bbea-1ef69e2f21e9" />
<br>
<br>
<img width="900" height="506" alt="3" src="https://github.com/user-attachments/assets/1ad2e0f5-d0d1-4815-a016-a005884143e0" />

## Why?
I enjoy quickly scrolling through the images captured by my telescope, adding the dimension of time to static frames, and watching satellite or airplane trails streak across the sensor's field of view. 
I believe that reviewing photos this way brings an extra layer of excitement to the experience. It is also a method for spotting astronomical transients and diagnosing underlying issues within the telescope or camera setup.
In the past, I relied on a combination of `ffmpeg` and `AstroImageJ` to achieve this. However, I wanted a more seamless, immediate solution directly integrated into [Siril](https://siril.org). 
As I am not a Python developer, I warmly encourage anyone with the patience and expertise to reach out and report any bugs or suggest improvements. 
Tested and working on **Debian GNU/Linux 13 (trixie)**. :-)

---
## 🤖 AI Development Disclaimer
This software was co-created through a human-AI collaboration. The architectural requirements, structural constraints (such as Siril's runtime path isolation, localized relative directory handling, granular cache multi-threading), debugging verification, and operational logic were directed and designed by the repository owner. The physical source code implementation was synthesized by an Artificial Intelligence assistant based on those precise specifications. 

---
## 📄 License
This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**. 
See the [LICENSE](LICENSE) file for the full text and details regarding permissions, conditions, and limitations.
