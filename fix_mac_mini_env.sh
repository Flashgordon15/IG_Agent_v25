#!/bin/bash
# Find and append common Node/npm paths to the environment
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -d "$HOME/.nvm" ]; then
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
fi
if [ -d "/opt/homebrew/bin" ]; then
    export PATH="/opt/homebrew/bin:$PATH"
fi
echo "Current PATH: $PATH"
echo "Testing npm version..."
npm -v || echo "ERROR: npm still not found. Please verify Node installation path manually."
