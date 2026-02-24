## Trial Run - Smart Home System

LLM used - ChatGPT 5.2 (Default Mode)

### Prompts for intended [model](../smart_home.png).

[Chat Transcript](https://chatgpt.com/share/699d5ba5-8bd0-8006-8ff9-b5167ddc59ae)

1. Generate a misuse case diagram in PlantUML for a Smart Home System based on the following requirements.
   - Req 1: The system shall allow the homeowner to unlock the door remotely.
   - Req 2: The system shall allow the homeowner to view the security camera feed.
   - Req 3: The system shall allow the homeowner to receive intrusion alerts.
   - Req 4: Under certain conditions, receiving an intrusion alert may trigger notifying emergency contacts.
   - Req 5: As part of unlocking the door remotely, the system shall always require biometric verification and encrypt device communication.
   - Req 6: As part of viewing the security camera, the system shall always encrypt the video stream.
   - Req 7: A burglar may hijack the smart lock, which undermines the remote door unlocking process.
   - Req 8: An eavesdropper may spy on the camera feed, which undermines the security camera viewing process.
   - Req 9: An eavesdropper may intercept device traffic, which undermines both the remote door unlocking process and the security camera viewing process.
   - Req 10: Requiring biometric verification shall serve as a countermeasure against smart lock hijacking.
   - Req 11: Encrypting the video stream shall serve as a countermeasure against spying on the camera feed.
   - Req 12: Encrypting device communication shall serve as a countermeasure against intercepting device traffic.

2. Please move all actors and misactors outside the system boundary rectangle, as actors should not be enclosed within the system context boundary in use case diagrams.

### Statistics

| Session | Construct Prompts | Visual Prompts | Total Prompts | Result  | Failure reason |
| ------- | ----------------- | -------------- | ------------- | ------- | -------------- |
| 1       | 1                 | 1              | 2             | Success |                |

### Final Output model

![Final Output](./output/2.png)
