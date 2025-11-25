#!/bin/bash

# Script d'arrêt du serveur 8mm Film Scanner

echo "8mm Film Scanner - Arrêt du serveur..."
echo ""

# Trouver et arrêter le processus Python du serveur
SERVER_PID=$(ps aux | grep "python.*server.py" | grep -v grep | awk '{print $2}')

if [ -z "$SERVER_PID" ]; then
    echo "✗ Aucun serveur en cours d'exécution"
    exit 0
fi

echo "Processus serveur trouvé (PID: $SERVER_PID)"
echo "Arrêt en cours..."

# Arrêter le serveur
sudo kill $SERVER_PID

# Attendre que le processus se termine
sleep 2

# Vérifier s'il tourne encore
if ps -p $SERVER_PID > /dev/null 2>&1; then
    echo "Le processus résiste, arrêt forcé..."
    sudo kill -9 $SERVER_PID
    sleep 1
fi

# Vérification finale
if ps aux | grep "python.*server.py" | grep -v grep > /dev/null; then
    echo "✗ Erreur : Le serveur tourne toujours"
    exit 1
else
    echo "✓ Serveur arrêté avec succès"
fi

echo ""
echo "Pour redémarrer : ./start_server.sh"
