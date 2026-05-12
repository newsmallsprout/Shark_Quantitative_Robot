package rl

import (
	"math"
	"math/rand"
)

// ── DQN Agent: Deep Q-Network with experience replay ──

// Simple 2-layer neural network for Q-value approximation
type QNetwork struct {
	W1     [][]float64 // input_dim × hidden_dim
	B1     []float64
	W2     [][]float64 // hidden_dim × output_dim
	B2     []float64
	Input  int
	Hidden int
	Output int
}

func NewQNetwork(input, hidden, output int) *QNetwork {
	nn := &QNetwork{
		Input:  input,
		Hidden: hidden,
		Output: output,
	}
	scale1 := math.Sqrt(2.0 / float64(input))
	scale2 := math.Sqrt(2.0 / float64(hidden))

	nn.W1 = make([][]float64, input)
	for i := 0; i < input; i++ {
		nn.W1[i] = make([]float64, hidden)
		for j := 0; j < hidden; j++ {
			nn.W1[i][j] = rand.NormFloat64() * scale1
		}
	}
	nn.B1 = make([]float64, hidden)

	nn.W2 = make([][]float64, hidden)
	for i := 0; i < hidden; i++ {
		nn.W2[i] = make([]float64, output)
		for j := 0; j < output; j++ {
			nn.W2[i][j] = rand.NormFloat64() * scale2
		}
	}
	nn.B2 = make([]float64, output)
	return nn
}

func (nn *QNetwork) Clone() *QNetwork {
	clone := &QNetwork{
		Input:  nn.Input,
		Hidden: nn.Hidden,
		Output: nn.Output,
	}
	clone.W1 = make([][]float64, nn.Input)
	for i := range nn.W1 {
		clone.W1[i] = make([]float64, nn.Hidden)
		copy(clone.W1[i], nn.W1[i])
	}
	clone.B1 = make([]float64, nn.Hidden)
	copy(clone.B1, nn.B1)
	clone.W2 = make([][]float64, nn.Hidden)
	for i := range nn.W2 {
		clone.W2[i] = make([]float64, nn.Output)
		copy(clone.W2[i], nn.W2[i])
	}
	clone.B2 = make([]float64, nn.Output)
	copy(clone.B2, nn.B2)
	return clone
}

func (nn *QNetwork) Forward(state []float64) []float64 {
	// hidden = ReLU(W1·state + B1)
	hidden := make([]float64, nn.Hidden)
	for j := 0; j < nn.Hidden; j++ {
		sum := nn.B1[j]
		for i, v := range state {
			if i < nn.Input {
				sum += v * nn.W1[i][j]
			}
		}
		if sum > 0 {
			hidden[j] = sum
		}
	}

	// output = W2·hidden + B2 (linear)
	output := make([]float64, nn.Output)
	for j := 0; j < nn.Output; j++ {
		sum := nn.B2[j]
		for i, h := range hidden {
			if i < nn.Hidden {
				sum += h * nn.W2[i][j]
			}
		}
		output[j] = sum
	}
	return output
}

type Experience struct {
	State     []float64
	Action    int
	Reward    float64
	NextState []float64
	Done      bool
}

type DQNAgent struct {
	QNet       *QNetwork
	TargetNet  *QNetwork
	Memory     []Experience
	MemSize    int
	MemIdx     int
	BatchSize  int
	Gamma      float64
	Epsilon    float64
	EpsilonMin float64
	EpsilonDec float64
	LR         float64
	UpdateFreq int
	StepCount  int

	StateDim  int
	ActionDim int
	rng       *rand.Rand
}

func NewDQNAgent(stateDim, actionDim int) *DQNAgent {
	hidden := 32
	agent := &DQNAgent{
		QNet:       NewQNetwork(stateDim, hidden, actionDim),
		TargetNet:  NewQNetwork(stateDim, hidden, actionDim),
		Memory:     make([]Experience, 2000),
		MemSize:    2000,
		BatchSize:  32,
		Gamma:      0.95,
		Epsilon:    1.0,
		EpsilonMin: 0.05,
		EpsilonDec: 0.9995,
		LR:         0.001,
		UpdateFreq: 100,
		StateDim:   stateDim,
		ActionDim:  actionDim,
		rng:        rand.New(rand.NewSource(123)),
	}
	// sync target net
	copyTarget(agent.TargetNet, agent.QNet)
	return agent
}

func copyTarget(dst, src *QNetwork) {
	for i := range src.W1 {
		copy(dst.W1[i], src.W1[i])
	}
	copy(dst.B1, src.B1)
	for i := range src.W2 {
		copy(dst.W2[i], src.W2[i])
	}
	copy(dst.B2, src.B2)
}

func (a *DQNAgent) Act(state []float64, training bool) Action {
	if training && a.rng.Float64() < a.Epsilon {
		return Action(a.rng.Intn(int(Close + 1)))
	}
	qvals := a.QNet.Forward(state)
	best := 0
	for i := 1; i < len(qvals); i++ {
		if qvals[i] > qvals[best] {
			best = i
		}
	}
	return Action(best)
}

func (a *DQNAgent) Remember(state []float64, action Action, reward float64, nextState []float64, done bool) {
	exp := Experience{
		State:     make([]float64, len(state)),
		Action:    int(action),
		Reward:    reward,
		NextState: make([]float64, len(nextState)),
		Done:      done,
	}
	copy(exp.State, state)
	copy(exp.NextState, nextState)
	a.Memory[a.MemIdx%a.MemSize] = exp
	a.MemIdx++
}

func (a *DQNAgent) Replay() {
	if a.MemIdx < a.BatchSize {
		return
	}

	validMem := a.Memory
	n := a.MemIdx
	if n > a.MemSize {
		n = a.MemSize
	}

	for b := 0; b < a.BatchSize; b++ {
		idx := a.rng.Intn(n)
		exp := validMem[idx%a.MemSize]
		if len(exp.State) == 0 {
			continue
		}

		// current Q
		qCurr := a.QNet.Forward(exp.State)
		if exp.Action >= len(qCurr) {
			continue
		}
		qOld := qCurr[exp.Action]

		// target Q
		qNext := a.TargetNet.Forward(exp.NextState)
		qMax := qNext[0]
		for _, v := range qNext {
			if v > qMax {
				qMax = v
			}
		}
		qTarget := qOld
		if exp.Done {
			qTarget = exp.Reward
		} else {
			qTarget = exp.Reward + a.Gamma*qMax
		}

		// gradient: MSE loss → update weights
		td := qTarget - qOld
		lr := a.LR

		// backprop through output layer
		hidden := make([]float64, a.QNet.Hidden)
		for j := 0; j < a.QNet.Hidden; j++ {
			sum := a.QNet.B1[j]
			for i, v := range exp.State {
				if i < a.QNet.Input {
					sum += v * a.QNet.W1[i][j]
				}
			}
			if sum > 0 {
				hidden[j] = sum
			}
		}

		// update W2, B2 for output neuron
		a.QNet.B2[exp.Action] += lr * td
		for i, h := range hidden {
			if i < a.QNet.Hidden {
				a.QNet.W2[i][exp.Action] += lr * td * h
			}
		}

		// backprop to hidden layer
		hiddenGrad := make([]float64, a.QNet.Hidden)
		for i := 0; i < a.QNet.Hidden; i++ {
			if hidden[i] > 0 { // ReLU derivative
				hiddenGrad[i] = lr * td * a.QNet.W2[i][exp.Action]
			}
		}

		// update W1, B1
		for i := 0; i < a.QNet.Hidden; i++ {
			a.QNet.B1[i] += hiddenGrad[i]
			for j, v := range exp.State {
				if j < a.QNet.Input {
					a.QNet.W1[j][i] += hiddenGrad[i] * v
				}
			}
		}
	}

	a.StepCount += a.BatchSize
	if a.StepCount >= a.UpdateFreq {
		copyTarget(a.TargetNet, a.QNet)
		a.StepCount = 0
	}

	// decay epsilon
	if a.Epsilon > a.EpsilonMin {
		a.Epsilon *= a.EpsilonDec
	}
}

// Save/Load Q-network weights as JSON
func (a *DQNAgent) ExportWeights() map[string]interface{} {
	return map[string]interface{}{
		"w1": a.QNet.W1,
		"b1": a.QNet.B1,
		"w2": a.QNet.W2,
		"b2": a.QNet.B2,
		"epsilon": a.Epsilon,
	}
}

func (a *DQNAgent) ImportWeights(data map[string]interface{}) {
	if w1, ok := data["w1"].([][]float64); ok {
		a.QNet.W1 = w1
		copyTarget(a.TargetNet, a.QNet)
	}
	if eps, ok := data["epsilon"].(float64); ok {
		a.Epsilon = eps
	}
}

// NumPy helper: soft update target network
func (a *DQNAgent) SoftUpdateTarget(tau float64) {
	for i := range a.QNet.W1 {
		for j := range a.QNet.W1[i] {
			a.TargetNet.W1[i][j] = tau*a.QNet.W1[i][j] + (1-tau)*a.TargetNet.W1[i][j]
		}
	}
	for i := range a.QNet.B1 {
		a.TargetNet.B1[i] = tau*a.QNet.B1[i] + (1-tau)*a.TargetNet.B1[i]
	}
	for i := range a.QNet.W2 {
		for j := range a.QNet.W2[i] {
			a.TargetNet.W2[i][j] = tau*a.QNet.W2[i][j] + (1-tau)*a.TargetNet.W2[i][j]
		}
	}
	for i := range a.QNet.B2 {
		a.TargetNet.B2[i] = tau*a.QNet.B2[i] + (1-tau)*a.TargetNet.B2[i]
	}
}
