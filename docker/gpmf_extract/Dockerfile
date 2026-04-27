FROM node:20-alpine

WORKDIR /app

# Newer gopro-telemetry handles Hero 13 KLV packets that crash the old
# chicheng/openicc image with "Error, negative length".
RUN npm install --omit=dev \
        gopro-telemetry \
        gpmf-extract \
    && npm cache clean --force

COPY extract.js /app/extract.js

ENTRYPOINT ["node", "/app/extract.js"]
