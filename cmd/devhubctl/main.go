// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Command devhubctl is the devhub command-line tool.
//
// See Run in run.go.
package main

import "os"

func main() { os.Exit(Run(os.Args[1:], streams{in: os.Stdin, out: os.Stdout, err: os.Stderr})) }
