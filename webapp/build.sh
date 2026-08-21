#!/bin/bash

# Render build script for MediSense
# Install system dependencies for OpenCV and Python packages

set -e  # Exit on error

echo "Installing system dependencies..."

# Update package manager
apt-get update

# Install system libraries needed for OpenCV
apt-get install -y \
    python3-dev \
    build-essential \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgtk-3-0 \
    libgl1-mesa-glx

echo "Installing Python dependencies..."
pip install --upgrade pip setuptools wheel

# Install from requirements
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
else
    echo "ERROR: requirements.txt not found!"
    exit 1
fi

echo "Build completed successfully!"
