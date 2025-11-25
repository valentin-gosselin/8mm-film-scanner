#!/bin/bash

# Script de démarrage du serveur 8mm Film Scanner

echo "8mm Film Scanner - Démarrage..."

# Activer l'environnement virtuel
source .venv/bin/activate

# Démarrer pigpiod si nécessaire
if ! pigs t > /dev/null 2>&1; then
    echo "Démarrage de pigpiod..."
    sudo pigpiod
    sleep 2
fi

# Démarrer le serveur
echo "Démarrage du serveur sur http://192.168.0.29:80"
echo "Appuyez sur Ctrl+C pour arrêter"
sudo -E env PATH=$PATH python3 server.py
