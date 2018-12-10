package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"net"
	"net/http"

	"github.com/containernetworking/cni/pkg/skel"
	"github.com/containernetworking/cni/pkg/types"
	"github.com/containernetworking/cni/pkg/types/current"
	"github.com/containernetworking/cni/pkg/version"
)

// TODO: configurable?
const remoteNetwork string = "unix"
const remoteAddress string = "/var/run/vlcp-k8s-cni/vlcp-k8s-cni.sock"

func parseConfig(stdin []byte) (*types.NetConf, error) {
	conf := types.NetConf{}

	if err := json.Unmarshal(stdin, &conf); err != nil {
		return nil, fmt.Errorf("failed to parse network configuration: %v", err)
	}
	return &conf, nil
}

func callRemoteService(path string, args *skel.CmdArgs) ([]byte, error) {
	client := &http.Client{
		Transport: &http.Transport{
			Dial: func(proto, addr string) (net.Conn, error) {
				return net.Dial(remoteNetwork, remoteAddress)
			},
		},
	}
	req, err := http.NewRequest("POST", "http://vlcp"+path, bytes.NewReader(args.StdinData))
	if err != nil {
		return nil, fmt.Errorf("failed to send CNI request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CNI-ContainerID", args.ContainerID)
	req.Header.Set("X-CNI-Netns", args.Netns)
	req.Header.Set("X-CNI-IfName", args.IfName)
	req.Header.Set("X-CNI-Args", args.Args)
	req.Header.Set("X-CNI-Path", args.Path)
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send CNI request: %v", err)
	}
	defer resp.Body.Close()

	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read CNI result: %v", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("CNI request failed with status %v: '%s'", resp.StatusCode, string(body))
	}
	return body, err
}

func delegateToRemote(path string, args *skel.CmdArgs, printResult bool) error {
	conf, err := parseConfig(args.StdinData)
	if err != nil {
		return err
	}

	body, err := callRemoteService(path, args)
	if err != nil {
		return err
	}
	if printResult {
		result, err := current.NewResult(body)
		if err != nil {
			return err
		}
		return types.PrintResult(result, conf.CNIVersion)
	} else {
		return err
	}
}

func cmdAdd(args *skel.CmdArgs) error {
	return delegateToRemote("/add", args, true)
}

func cmdDel(args *skel.CmdArgs) error {
	return delegateToRemote("/del", args, false)
}

func main() {
	// TODO: implement plugin version
	skel.PluginMain(cmdAdd, cmdGet, cmdDel, version.All, "CNI plugin vlcp-k8s-cni v0.1.0")
}

func cmdGet(args *skel.CmdArgs) error {
	// TODO: implement
	return fmt.Errorf("not implemented")
}
