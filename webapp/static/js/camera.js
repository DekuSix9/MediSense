// MediSense Browser Camera Capture & Detection Sync Script

document.addEventListener("DOMContentLoaded", () => {
    const video = document.getElementById("camera");
    const canvas = document.getElementById("snapshot");
    const cameraStatusBadge = document.getElementById("camera-status");
    const wrongLidBanner = document.getElementById("wrong-lid-alert");
    const wrongLidText = document.getElementById("wrong-lid-text");
    const expectedDoseSpan = document.getElementById("expected-dose");

    if (!video || !canvas) return;

    // Start browser camera stream
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        navigator.mediaDevices.getUserMedia({ video: true })
            .then(stream => {
                video.srcObject = stream;
                if (cameraStatusBadge) {
                    cameraStatusBadge.textContent = "Camera Live";
                    cameraStatusBadge.className = "badge badge-live";
                }
            })
            .catch(err => {
                console.error("Camera access error:", err);
                if (cameraStatusBadge) {
                    cameraStatusBadge.textContent = "Camera Blocked";
                    cameraStatusBadge.className = "badge badge-info";
                }
            });
    }

    function captureFrame() {
        if (!video.videoWidth || !video.videoHeight) return;

        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const imageData = canvas.toDataURL("image/jpeg", 0.7);

        fetch("/detect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image: imageData })
        })
        .then(response => response.json())
        .then(data => {
            if (!data) return;

            // Update expected dose label
            if (expectedDoseSpan && data.expected_dose) {
                expectedDoseSpan.textContent = data.expected_dose;
            }

            // Update compartment cards UI if available
            if (data.last_state) {
                ["Morning", "Afternoon", "Night"].forEach(dose => {
                    const statusElem = document.getElementById(`status-${dose.toLowerCase()}`);
                    const cardElem = document.getElementById(`card-${dose.toLowerCase()}`);
                    if (statusElem) {
                        const isOpen = data.last_state[dose];
                        statusElem.textContent = isOpen ? "OPENED" : "CLOSED";
                        statusElem.className = isOpen ? "dose-status status-open" : "dose-status status-closed";
                    }
                    if (cardElem) {
                        if (data.expected_dose === dose) {
                            cardElem.classList.add("active-window");
                        } else {
                            cardElem.classList.remove("active-window");
                        }
                    }
                });
            }
        })
        .catch(err => {
            console.error("Frame upload error:", err);
        });
    }

    // Send frame twice per second (every 500ms) as specified in build plan
    setInterval(captureFrame, 500);
});
