package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
)

type contractFile struct {
	TestVector struct {
		Secret  string `json:"secret"`
		Request struct {
			Body string `json:"body"`
		} `json:"request"`
		ContentSHA256 string `json:"content_sha256"`
		Canonical     string `json:"canonical"`
		Signature     string `json:"signature"`
	} `json:"test_vector"`
}

func main() {
	payload, err := os.ReadFile("tests/contract/oac_irs/identity/v2/contract.json")
	if err != nil {
		panic(err)
	}
	var contract contractFile
	if err := json.Unmarshal(payload, &contract); err != nil {
		panic(err)
	}
	vector := contract.TestVector
	bodyHash := sha256.Sum256([]byte(vector.Request.Body))
	if hex.EncodeToString(bodyHash[:]) != vector.ContentSHA256 {
		panic("body SHA-256 mismatch")
	}
	mac := hmac.New(sha256.New, []byte(vector.Secret))
	_, _ = mac.Write([]byte(vector.Canonical))
	actual := "v2=" + hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(actual), []byte(vector.Signature)) {
		panic("Go HMAC vector mismatch")
	}
	fmt.Println("Go HMAC 向量检查通过")
}
