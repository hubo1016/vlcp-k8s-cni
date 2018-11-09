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

func callRemoteService(path string, args *skel.CmdArgs) (*types.Result, error){
	client := &http.Client{
		Transport: &http.Transport{
			Dial: func(proto, addr string) (net.Conn, error) {
				return net.Dial(remoteNetwork, remoteAddress)
			},
		},
	}

	resp, err := client.Post("http://vlcp" + path, "application/json", bytes.NewReader(args.StdinData))
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
	result, err := current.NewResult(body)
	return &result, err
}

func delegateToRemote(path string, args *skel.CmdArgs) error {
	conf, err := parseConfig(args.StdinData)
	if err != nil {
		return err
	}
	
	result, err := callRemoteService(path, args)
	if err != nil{
		return err
	}
	return types.PrintResult(*result, conf.CNIVersion)	
}

func cmdAdd(args *skel.CmdArgs) error {
	return delegateToRemote("/add", args)
}

func cmdDel(args *skel.CmdArgs) error {
	return delegateToRemote("/del", args)
}

func main() {
	// TODO: implement plugin version
	skel.PluginMain(cmdAdd, cmdGet, cmdDel, version.All, "CNI plugin vlcp-k8s-cni v0.1.0")
}

func cmdGet(args *skel.CmdArgs) error {
	// TODO: implement
	return fmt.Errorf("not implemented")
}
