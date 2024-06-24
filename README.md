# dcase2024_task2

Submission for task 2 ["First-Shot Unsupervised Anomalous Sound Detection for Machine Condition Monitoring"](https://dcase.community/challenge2024/task-first-shot-unsupervised-anomalous-sound-detection-for-machine-condition-monitoring) of the DCASE2024 Challenge. The system is an adaptation of the self-supervised learning based [ASD system](https://github.com/wilkinghoff/ssl4asd) specifically designed for domain generalization and uses the [AdaProj Loss](https://github.com/wilkinghoff/AdaProj) as well as balanced class weights.

# Instructions
The implementation is based on Tensorflow 2.3 (more recent versions can run into problems with the current implementation). Just start the main.py script for training and evaluation. To run the code, you need to download the development dataset, additional training dataset and the evaluation dataset, and store the files in an './eval_data' and a './dev_data' folder.

# Reference
When finding this code helpful, or reusing parts of it, a citation would be appreciated:
@techreport{wilkinghoff2024challenge_t2,
  author = {Wilkinghoff, Kevin and Bel-Hadj, Yacine},
  title  = {{FKIE-VUB} System for {DCASE2024} Challenge Task 2: {F}irst-Shot Unsupervised Anomalous Sound Detection for Machine Condition Monitoring},
  institution   = {DCASE2024 Challenge},
  year   = {2024}
}
